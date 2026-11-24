#!/usr/bin/env python3
"""Tell the site owner about new accounts and new message-board posts.

Runs from .github/workflows/notify-activity.yml every 15 minutes. It asks the database for the
current accounts and the latest board posts, compares them with the ids it already reported
(lineups/notify_state.json) and, if anything is new, writes a short message that the workflow
turns into a GitHub issue (GitHub then emails / pushes it to the repository owner).

The first run only records what already exists, so you are not flooded with old activity.
No e-mail addresses are ever put in the message (the repository is public).
"""
import json, os, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "lineups", "notify_state.json")
OUT = os.environ.get("RUNNER_TEMP", "/tmp")
URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
KEEP = 3000


def call(path):
    req = urllib.request.Request(URL + path, headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def accounts():
    out, page = [], 1
    while page <= 20:
        d = call("/auth/v1/admin/users?page=%d&per_page=200" % page)
        batch = d.get("users", []) if isinstance(d, dict) else (d or [])
        out += batch
        if len(batch) < 200:
            break
        page += 1
    return out


def posts():
    for order in ("&order=created_at.desc", ""):
        try:
            return call("/rest/v1/lineup_posts?select=*" + order + "&limit=200") or []
        except urllib.error.HTTPError as e:
            print("posts query failed:", e.code, "(order: %s)" % (order or "none"))
    return []


def team_name(key):
    key = str(key or "")
    for pre in ("whl_", "qmjhl_"):
        if key.startswith(pre):
            return "%s %s" % (key[len(pre):].replace("_", " ").title(), pre[:-1].upper())
    return "%s OHL" % key.replace("_", " ").title()


def main():
    if not URL or not KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set; nothing to do")
        return
    st = json.load(open(STATE)) if os.path.exists(STATE) else None
    first = st is None
    st = st or {"users": [], "posts": []}
    us, ps = accounts(), posts()
    print("accounts:", len(us), "| board posts read:", len(ps), "| first run:", first)
    if ps:
        print("post columns:", sorted(ps[0].keys()))
    seen_u, seen_p = set(st["users"]), set(st["posts"])
    new_u = [u for u in us if str(u.get("id")) not in seen_u]
    new_p = [p for p in ps if str(p.get("id")) not in seen_p]
    if not first and (new_u or new_p):
        bits = []
        if new_u:
            bits.append("%d new account%s" % (len(new_u), "" if len(new_u) == 1 else "s"))
        if new_p:
            bits.append("%d new board post%s" % (len(new_p), "" if len(new_p) == 1 else "s"))
        lines = []
        if new_u:
            lines.append("## New accounts")
            for u in sorted(new_u, key=lambda x: str(x.get("created_at") or "")):
                lines.append("- created %s UTC" % str(u.get("created_at") or "?")[:16].replace("T", " "))
            lines.append("")
            lines.append("Total accounts now: %d" % len(us))
            lines.append("")
        if new_p:
            lines.append("## New message-board posts")
            for p in sorted(new_p, key=lambda x: str(x.get("created_at") or "")):
                body = " ".join(str(p.get("body") or "").split())
                if len(body) > 300:
                    body = body[:300] + "..."
                kind = "reply" if p.get("parent_id") else "post"
                lines.append("- **%s** (%s) [open board](https://topsixhockey.ca/?team=%s): %s" % (team_name(p.get("team_key")), kind, p.get("team_key"), body))
        open(os.path.join(OUT, "notify_title.txt"), "w").write("Top Six Hockey: " + ", ".join(bits))
        open(os.path.join(OUT, "notify_body.md"), "w").write("\n".join(lines) + "\n")
        print("notification written:", ", ".join(bits))
    elif new_u or new_p:
        print("first run: recording %d accounts and %d posts without notifying" % (len(new_u), len(new_p)))
    else:
        print("nothing new")
    st["users"] = (st["users"] + [str(u.get("id")) for u in new_u])[-KEEP:]
    st["posts"] = (st["posts"] + [str(p.get("id")) for p in new_p])[-KEEP:]
    if first or new_u or new_p:
        json.dump(st, open(STATE, "w"))


if __name__ == "__main__":
    main()
