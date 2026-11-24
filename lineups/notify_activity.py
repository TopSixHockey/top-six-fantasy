#!/usr/bin/env python3
"""Tell the site owner about new activity on the site.

Runs from .github/workflows/notify-activity.yml every 15 minutes. It reads the database for
  - new accounts,
  - new message-board posts,
  - new public teams (free game entries),
  - new private leagues,
compares them with the ids it already reported (lineups/notify_state.json) and, if anything is
new, writes a short message that the workflow turns into a GitHub issue (GitHub then e-mails and
pushes it to the repository owner).

The first time a kind of activity is seen it is only recorded, so you are not flooded with old rows.
The repository is public, so no e-mail addresses and no private-league names go into the message.
"""
import json, os, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

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


def table(name):
    """All rows of a table (newest first when it has created_at); None if it cannot be read."""
    for order in ("&order=created_at.desc", ""):
        try:
            return call("/rest/v1/%s?select=*%s&limit=1000" % (name, order)) or []
        except urllib.error.HTTPError as e:
            print("%s query failed: %s (order: %s)" % (name, e.code, order or "none"))
    return None


def when(r):
    s = str(r.get("created_at") or "")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).strftime("%b %d, %I:%M %p ET")
    except Exception:
        return s[:16] or "?"


def team_name(key):
    key = str(key or "")
    for pre in ("whl_", "qmjhl_"):
        if key.startswith(pre):
            return "%s %s" % (key[len(pre):].replace("_", " ").title(), pre[:-1].upper())
    return "%s OHL" % key.replace("_", " ").title()


def plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def main():
    if not URL or not KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set; nothing to do")
        return
    st = json.load(open(STATE)) if os.path.exists(STATE) else {}
    try:
        acc = accounts()
    except Exception as e:
        print("accounts unavailable:", e)
        acc = None
    sources = {"users": acc, "posts": table("lineup_posts"), "entries": table("entries"), "leagues": table("pleagues")}
    new, changed = {}, False
    for k, rows in sources.items():
        if rows is None:
            print(k, "unavailable; skipped")
            continue
        fresh = k not in st
        seen = set(st.get(k) or [])
        n = [r for r in rows if str(r.get("id")) not in seen]
        print("%s: %d rows | %d new%s" % (k, len(rows), len(n), " (first time: recorded silently)" if fresh else ""))
        if rows and fresh:
            print("  columns:", sorted(rows[0].keys()))
        if not fresh:
            new[k] = n
        st[k] = (list(st.get(k) or []) + [str(r.get("id")) for r in n])[-KEEP:]
        changed = changed or fresh or bool(n)
    new = {k: v for k, v in new.items() if v}
    if new:
        bits, lines = [], []
        by_time = lambda x: str(x.get("created_at") or "")
        if new.get("users"):
            n = new["users"]
            bits.append(plural(len(n), "new account"))
            lines += ["## New accounts"] + ["- created %s" % when(u) for u in sorted(n, key=by_time)]
            lines += ["", "Total accounts now: %d" % len(sources["users"]), ""]
        if new.get("entries"):
            n = new["entries"]
            bits.append(plural(len(n), "new public team"))
            lines += ["## New public teams (free game)"]
            for e in sorted(n, key=by_time):
                lines.append("- **%s** (%s %s)%s" % (str(e.get("team_name") or "unnamed")[:60], e.get("league") or "?", e.get("contest") or "", ", created " + when(e) if e.get("created_at") else ""))
            lines.append("")
        if new.get("leagues"):
            n = new["leagues"]
            bits.append(plural(len(n), "new private league"))
            lines += ["## New private leagues"] + ["- created %s" % when(l) for l in sorted(n, key=by_time)]
            lines += ["", "Private leagues now: %d (names are not shown here because this repository is public)" % len(sources["leagues"]), ""]
        if new.get("posts"):
            n = new["posts"]
            bits.append(plural(len(n), "new board post"))
            lines.append("## New message-board posts")
            for p in sorted(n, key=by_time):
                body = " ".join(str(p.get("body") or "").split())
                if len(body) > 300:
                    body = body[:300] + "..."
                kind = "reply" if p.get("parent_id") else "post"
                lines.append("- **%s** (%s) [open board](https://topsixhockey.ca/?team=%s): %s" % (team_name(p.get("team_key")), kind, p.get("team_key"), body))
        open(os.path.join(OUT, "notify_title.txt"), "w").write("Top Six Hockey: " + ", ".join(bits))
        open(os.path.join(OUT, "notify_body.md"), "w").write("\n".join(lines) + "\n")
        print("notification written:", ", ".join(bits))
    else:
        print("nothing new to report")
    if changed:
        json.dump(st, open(STATE, "w"))


if __name__ == "__main__":
    main()
