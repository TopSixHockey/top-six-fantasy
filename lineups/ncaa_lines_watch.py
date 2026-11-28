#!/usr/bin/env python3
"""NCAA lines watcher - DRY RUN ONLY (it never edits index.html).

For each team in lineups/ncaa_x_accounts.json it:
  1. reads the team's recent X posts that have a photo (X API, app-only Bearer token),
  2. asks Claude to read the photo as a lineup graphic,
  3. checks the result (4 lines of 3 forwards, 3 pairs of defence, goalies, no duplicate numbers),
  4. compares it with the lines that are currently in index.html and reports the differences.

Secrets (GitHub Actions): X_BEARER_TOKEN, ANTHROPIC_API_KEY. Neither is ever printed.
Output: a console log, the GitHub job summary, and lineups/_watch_report.json.
"""
import argparse, base64, datetime, json, os, re, sys, urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.environ.get("LINES_MODEL", "claude-sonnet-5-5")
ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

PROMPT = """You are reading a college hockey team's game-day lineup graphic posted on X by @@HANDLE@@ (post text: "@@TEXT@@").
Decide whether the image is a lineup (forward lines, defence pairs, goalies) for an upcoming or current game. Season previews, final scores, stat graphics, schedules, promos and ordinary photos are NOT lineups.
Return ONLY a JSON object, with no other text, in this shape:
{"is_lineup": true or false,
 "what": "a few words saying what the image is, e.g. lineup, final score graphic, promo, photo",
 "opponent": "school name as shown, or null",
 "home_away": "vs" or "at" or null,
 "game_date": "YYYY-MM-DD" or null,
 "F": up to 4 forward lines, each a list of 3 players in the order shown, each player [number, "First Last"],
 "D": defence pairs, each a list of 2 players; a lone 7th defenceman goes last as a one-player list,
 "G": goalies in the order shown (starter first), each [number, "First Last"],
 "S": extras, scratches or non-dressing players if the graphic lists them (otherwise []), each [number, "First Last"],
 "C": captain's jersey number or null,
 "A": list of assistant captain jersey numbers,
 "uncertain": ["short note for anything hard to read"]}
Rules: copy jersey numbers and name spellings exactly as shown. Never guess: if a number or name is unreadable use null for it and say so in "uncertain". Do not invent players. If the graphic shows a 13th forward, put him in "S". Use null for home_away when unclear ("vs" means home, "at" means away). If is_lineup is false, the other fields may be null."""


def http(url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s from %s: %s" % (e.code, url.split("?")[0], e.read()[:300].decode("utf-8", "replace")))


# ---------- X ----------
def xget(path, params):
    tok = os.environ["X_BEARER_TOKEN"]
    body, _ = http("https://api.x.com/2" + path + "?" + urllib.parse.urlencode(params), {"Authorization": "Bearer " + tok, "User-Agent": "TopSixLinesWatcher"})
    return json.loads(body)


def recent_photo_posts(handle, since, extra="", limit=20):
    """Posts by the account that have an attachment, newest first. Uses recent search, so X bills only for the posts returned."""
    q = ("from:%s has:media -is:retweet -is:reply %s" % (handle, extra)).strip()
    d = xget("/tweets/search/recent", {"query": q, "max_results": max(10, min(limit, 100)), "start_time": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       "tweet.fields": "created_at", "expansions": "attachments.media_keys", "media.fields": "type,url"})
    media = {m["media_key"]: m for m in d.get("includes", {}).get("media", [])}
    out, withmedia = [], 0
    for t in d.get("data", []):
        keys = t.get("attachments", {}).get("media_keys", [])
        withmedia += 1 if keys else 0
        imgs = [media[k]["url"] for k in keys if k in media and media[k].get("type") == "photo" and media[k].get("url")]
        if imgs:
            out.append({"id": t["id"], "created": t["created_at"], "text": t.get("text", ""), "images": imgs})
    return out, {"posts": len(d.get("data", [])), "with_media": withmedia, "types": sorted({str(m.get("type")) for m in media.values()})}  # newest first


# ---------- Claude ----------
def fetch_image(url):
    for size in ("large", "medium"):
        body, hdr = http(url + ("&" if "?" in url else "?") + "name=" + size, {"User-Agent": UA})
        if len(body) < 4900000:
            break
    ct = (hdr.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    return body, ct if ct in ("image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg"


def read_lineup(img, media_type, handle, text):
    prompt = PROMPT.replace("@@HANDLE@@", handle).replace("@@TEXT@@", re.sub(r"\s+", " ", text)[:200].replace('"', "'"))
    payload = {"model": MODEL, "max_tokens": 2500, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(img).decode()}},
        {"type": "text", "text": prompt}]}]}
    body, _ = http("https://api.anthropic.com/v1/messages", {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                   json.dumps(payload).encode(), 180)
    r = json.loads(body)
    txt = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    return (json.loads(m.group(0)) if m else None), r.get("usage", {})


# ---------- what index.html has now ----------
def field(block, name):
    m = re.search(r"\n\s+" + name + r": ", block)
    if not m:
        return None
    rest = block[m.end():]
    n = re.search(r",?\s+[A-Za-z]+: ", rest)
    return json.loads((rest[:n.start()] if n else rest).strip().rstrip(","))


def load_current(html, key):
    i = html.find("var NCHC_DATA = ")
    if i >= 0:
        line = html[i + len("var NCHC_DATA = "):html.index("\n", i)].rstrip().rstrip(";")
        for t in json.loads(line).values():
            if t.get("k") == key:
                return {"F": t["F"], "D": t["D"], "G": t["G"], "S": t["S"], "game": t.get("game", ""), "roster": {int(r[0]) for r in t["p"]}}
    mj = re.search(r'var K = "' + key + r'";\s*\n\s*L\[K\] = (\{.*\});\n[^\n]*\n([^\n]*)\n', html)
    if mj:
        o = json.loads(mj.group(1))
        return {"F": o["F"], "D": o["D"], "G": o["G"], "S": o.get("S") or [], "game": o.get("game", ""), "roster": {int(x) for x in re.findall(r'\[(\d+),"[FDG]"', mj.group(2))}}
    m = re.search(r"\n  L\." + key + r" = \{(.*?)\n  \};", html, re.S)
    if not m:
        return None
    b = m.group(1)
    g = re.search(r'game: "(.*?)", updated', b)
    roster = set()
    for pm in re.finditer(r'put\("' + key + r'", \[(.*?)\n  \]\);', html, re.S):
        roster |= {int(x) for x in re.findall(r'\[(\d+),"[FDG]"', pm.group(1))}
    return {"F": field(b, "F"), "D": field(b, "D"), "G": field(b, "G"), "S": field(b, "S") or [], "game": g.group(1) if g else "", "roster": roster}


def nums(groups):
    return [[p[0] for p in g] for g in groups]


def flat(lu):
    """every player [number, name] in a lineup (F and D are lists of groups; G and S are flat lists)"""
    return [p for grp in (lu.get("F") or []) + (lu.get("D") or []) for p in grp] + list(lu.get("G") or []) + list(lu.get("S") or [])


def check(lu):
    bad = []
    try:
        F, D, G = lu.get("F") or [], lu.get("D") or [], lu.get("G") or []
        if len(F) != 4 or any(len(x) != 3 for x in F):
            bad.append("forwards are not 4 lines of 3")
        if len(D) not in (3, 4) or any(len(x) != 2 for x in D[:3]) or (len(D) == 4 and len(D[3]) != 1):
            bad.append("defence is not 3 pairs (+ optional lone 7th)")
        if not 1 <= len(G) <= 3:
            bad.append("%d goalies" % len(G))
        ns = [p[0] for p in flat(lu)]
        if any(not isinstance(n, int) for n in ns):
            bad.append("a jersey number is missing or unreadable")
        elif len(set(ns)) != len(ns):
            bad.append("a jersey number appears twice")
    except (TypeError, IndexError, KeyError):
        bad.append("the result is not in the expected shape")
    return bad


def pdate(created):
    return datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(ET).date()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--teams", default="")
    ap.add_argument("--max-images", type=int, default=5, help="most photos to read per team")
    ap.add_argument("--extra", default="", help="extra X search terms, e.g. (lines OR lineup)")
    ap.add_argument("--limit", type=int, default=20, help="most posts to fetch per team")
    ap.add_argument("--explore", action="store_true", help="only list each team's recent media posts (no Claude)")
    a = ap.parse_args()
    acc = {k: v for k, v in json.load(open(os.path.join(HERE, "ncaa_x_accounts.json"))).items() if not k.startswith("_")}
    want = {x.strip() for x in a.teams.split(",") if x.strip()}
    if want:
        acc = {k: v for k, v in acc.items() if k in want}
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=a.hours)
    print("Looking back %g hours from %s UTC; model %s\n" % (a.hours, since.strftime("%Y-%m-%d %H:%M"), MODEL))
    rep, tin, tout, nimg = [], 0, 0, 0
    for key, handle in acc.items():
        row = {"team": key, "handle": handle, "status": "", "detail": ""}
        rep.append(row)
        try:
            cur = load_current(html, key)
            if not cur:
                row["status"], row["detail"] = "ERROR", "team not found in index.html"
                continue
            posts, stats = recent_photo_posts(handle, since, a.extra, a.limit)
            row["fetched"] = stats
            if a.explore:
                row["status"] = "explore"
                row["detail"] = "%d media posts" % stats["posts"]
                row["posts"] = ["%s | %d photo(s) | %s" % (pdate(x["created"]), len(x["images"]), re.sub(r"\s+", " ", x["text"])[:120]) for x in posts]
                continue
            if not posts:
                row["status"] = "no photo posts"
                row["detail"] = "%d posts, %d with media (media types: %s)" % (stats["posts"], stats["with_media"], ", ".join(stats["types"]) or "none")
                continue
            seen = 0
            for p in posts:
                for url in p["images"]:
                    if seen >= a.max_images:
                        break
                    seen += 1
                    img, mt = fetch_image(url)
                    lu, use = read_lineup(img, mt, "@" + handle, p["text"])
                    nimg += 1
                    row.setdefault("saw", []).append((lu or {}).get("what"))
                    tin += use.get("input_tokens", 0)
                    tout += use.get("output_tokens", 0)
                    if lu and lu.get("is_lineup"):
                        row["post"] = "https://x.com/%s/status/%s" % (handle, p["id"])
                        row["image"] = url
                        row["posted"] = str(pdate(p["created"]))
                        row["opponent"], row["home_away"], row["game_date"] = lu.get("opponent"), lu.get("home_away"), lu.get("game_date")
                        row["uncertain"] = lu.get("uncertain") or []
                        bad = check(lu)
                        if bad:
                            row["status"], row["detail"] = "NEEDS REVIEW", "; ".join(bad)
                            row["raw"] = lu
                        else:
                            diffs = []
                            if nums(lu["F"]) != nums(cur["F"]):
                                diffs.append("forwards")
                            if nums(lu["D"]) != nums(cur["D"]):
                                diffs.append("defence")
                            if [p[0] for p in lu["G"]] != [p[0] for p in cur["G"]]:
                                diffs.append("goalies")
                            if lu.get("S") and {p2[0] for p2 in lu["S"]} != {p2[0] for p2 in cur["S"]}:
                                diffs.append("scratches")
                            new = sorted({p2[0] for p2 in flat(lu)} - cur["roster"]) if cur["roster"] else []
                            row["new_numbers"] = new
                            row["status"] = "CHANGED" if diffs else "unchanged"
                            row["detail"] = ("differs in: " + ", ".join(diffs)) if diffs else "matches the site"
                            if new:
                                row["detail"] += "; numbers not on the site roster: %s" % new
                            row["lineup"] = {k: lu.get(k) for k in ("F", "D", "G", "S", "C", "A")}
                            row["current_game"] = cur["game"]
                        break
                if row["status"]:
                    break
            if not row["status"]:
                row["status"] = "no lineup found"
                row["detail"] = "read %d photo(s), none was a lineup (saw: %s)" % (seen, "; ".join(str(x) for x in row.get("saw", [])))
        except Exception as e:
            row["status"], row["detail"] = "ERROR", str(e)[:300]
        print("%-24s %-14s %s" % (key, row["status"], row["detail"]))
    print("\nPhotos read: %d, Claude tokens: %d in / %d out" % (nimg, tin, tout))
    json.dump({"generated": datetime.datetime.utcnow().isoformat() + "Z", "model": MODEL, "photos_read": nimg, "tokens_in": tin, "tokens_out": tout, "teams": rep},
              open(os.path.join(HERE, "_watch_report.json"), "w"), indent=1)
    sm = os.environ.get("GITHUB_STEP_SUMMARY")
    if sm:
        with open(sm, "a") as f:
            f.write("### NCAA lines watcher (dry run)\n\nPhotos read: %d, tokens %d in / %d out\n\n| Team | Status | Detail | Post |\n|---|---|---|---|\n" % (nimg, tin, tout))
            for r in rep:
                f.write("| %s | %s | %s | %s |\n" % (r["team"], r["status"], r["detail"].replace("|", "/"), r.get("post", "")))
            for r in rep:
                if r.get("posts"):
                    f.write("\n<b>%s - media posts</b>\n<pre>%s</pre>\n" % (r["team"], "\n".join(r["posts"]).replace("&", "&amp;").replace("<", "&lt;")))
                if r.get("lineup") and r["status"] == "CHANGED":
                    f.write("\n<b>%s - read from %s</b>\n<pre>%s</pre>\n" % (r["team"], r.get("image", ""), json.dumps(r["lineup"])[:2500].replace("&", "&amp;").replace("<", "&lt;")))
                if r.get("raw") is not None:
                    f.write("\n<b>%s - what Claude returned</b>\n<pre>%s</pre>\n" % (r["team"], json.dumps(r["raw"])[:2500].replace("&", "&amp;").replace("<", "&lt;")))


if __name__ == "__main__":
    main()
