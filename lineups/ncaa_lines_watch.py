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
from itertools import zip_longest
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.environ.get("LINES_MODEL", "claude-sonnet-5-5")
ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
# Words that teams use in their lineup posts. Only matching posts are fetched, so X bills only for those.
DEFAULT_EXTRA = '(lines OR lineup OR lineups OR "hit the ice" OR crew)'

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
A graphic that shows only the starting six (3 forwards, 2 defence, 1 goalie) is NOT a lineup: set is_lineup to false.
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
    q = ("from:%s has:images -is:retweet -is:reply %s" % (handle, extra)).strip()
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
                return {"F": t["F"], "D": t["D"], "G": t["G"], "S": t["S"], "game": t.get("game", ""), "roster": {int(r[0]) for r in t["p"]}, "fmt": "nchc"}
    mj = re.search(r'var K = "' + key + r'";\s*\n\s*L\[K\] = (\{.*\});\n[^\n]*\n([^\n]*)\n', html)
    if mj:
        o = json.loads(mj.group(1))
        return {"F": o["F"], "D": o["D"], "G": o["G"], "S": o.get("S") or [], "game": o.get("game", ""), "roster": {int(x) for x in re.findall(r'\[(\d+),"[A-Z]{1,2}"', mj.group(2))}, "fmt": "b"}
    m = re.search(r"\n  L\." + key + r" = \{(.*?)\n  \};", html, re.S)
    if not m:
        return None
    b = m.group(1)
    g = re.search(r'game: "(.*?)", updated', b)
    roster = set()
    for pm in re.finditer(r'put\("' + key + r'", \[(.*?)\n  \]\);', html, re.S):
        roster |= {int(x) for x in re.findall(r'\[(\d+),"[A-Z]{1,2}"', pm.group(1))}
    return {"F": field(b, "F"), "D": field(b, "D"), "G": field(b, "G"), "S": field(b, "S") or [], "game": g.group(1) if g else "", "roster": roster, "fmt": "a"}


def nums(groups):
    return [[p[0] for p in g] for g in groups]


def flat(lu):
    """every player [number, name] in a lineup (F and D are lists of groups; G and S are flat lists)"""
    return [p for grp in (lu.get("F") or []) + (lu.get("D") or []) for p in grp] + list(lu.get("G") or []) + list(lu.get("S") or [])


# ---------- writing lines into index.html (used only with --apply) ----------
def fmt_date(d):
    return d.strftime("%b ") + str(d.day) + d.strftime(", %Y")


def parse_game_date(game):
    m = re.search(r"\u00b7 (\w+ \d+, \d{4})", game or "")
    try:
        return datetime.datetime.strptime(m.group(1), "%b %d, %Y").date() if m else None
    except ValueError:
        return None


def js(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def names_of(cur):
    m = {}
    for grp in cur["F"] + cur["D"]:
        for q in grp:
            m[q[0]] = [q[1], q[2]]
    for q in cur["G"] + cur["S"]:
        m[q[0]] = [q[1], q[2]]
    return m


def build_lines(lu, cur):
    """New F, D, G, S in the site's shape [number, first, last]. Names already on the site are kept as they are spelled there."""
    nm, flagged = names_of(cur), []

    def pl(q):
        n, full = q[0], str(q[1] or "")
        if n in nm:
            return [n, nm[n][0], nm[n][1]]
        parts = full.split(" ", 1)
        flagged.append("%s %s" % (n, full))
        return [n, parts[0], parts[1] if len(parts) > 1 else ""]

    F, D = [[pl(q) for q in g] for g in lu["F"]], [[pl(q) for q in g] for g in lu["D"]]
    G = [pl(q) for q in lu["G"]]
    dressed = {q[0] for g in F + D for q in g} | {q[0] for q in G}
    S = [pl(q) for q in lu.get("S") or []]
    seen = {q[0] for q in S} | dressed
    old = [q for g in cur["F"] + cur["D"] for q in g] + cur["G"] + cur["S"]
    S += [[q[0], q[1], q[2]] for q in old if q[0] not in seen]  # keep players the site already lists as scratches (or dressed last time)
    return F, D, G, sorted(S, key=lambda q: q[0]), flagged


def tline(g):
    return ", ".join("%s %s" % (q[0], q[2] or q[1]) for q in g)


def diffs(old, new, label):
    out = []
    for i, (o, n) in enumerate(zip_longest(old, new)):
        if [q[0] for q in o or []] != [q[0] for q in n or []]:
            out.append("- %s %d: %s -> %s" % (label, i + 1, tline(o) if o else "(none)", tline(n) if n else "(none)"))
    return out


def write_team(html, key, fmt, F, D, G, S, game, updated, note, cap):
    if fmt == "nchc":
        i = html.index("var NCHC_DATA = ") + len("var NCHC_DATA = ")
        j = html.index("\n", i)
        line = html[i:j]
        data = json.loads(line.rstrip().rstrip(";"))
        if js(data) + ";" != line.rstrip():
            raise ValueError("NCHC data is not in the compact form I expect")
        for t in data.values():
            if t.get("k") == key:
                t.update({"F": F, "D": D, "G": G, "S": S, "game": game, "note": note})
                t.pop("score", None)
                if cap:
                    t["cap"] = cap
                break
        else:
            raise ValueError("team not in NCHC data")
        return html[:i] + js(data) + ";" + html[j:]
    if fmt == "b":
        m = re.search(r'(var K = "' + key + r'";\s*\n\s*L\[K\] = )(\{.*\})(;\n)', html)
        o = json.loads(m.group(2))
        if js(o) != m.group(2) or not set(o) <= {"game", "updated", "note", "F", "D", "G", "S", "score"}:
            raise ValueError("lines block is not in the form I expect")
        o.update({"F": F, "D": D, "G": G, "S": S, "game": game, "updated": updated, "note": note})
        o.pop("score", None)
        return html[:m.start(2)] + js(o) + html[m.end(2):]
    m = re.search(r"\n  L\." + key + r" = \{(.*?)\n  \};", html, re.S)
    if not m or not set(re.findall(r"(?:^|\n)    ([A-Za-z]+): ", m.group(1))) <= {"game", "updated", "note", "F", "D", "G", "S"}:
        raise ValueError("lines block is not in the form I expect")
    rows = ",\n".join("      " + js(g) for g in F)
    blk = '\n    game: %s, updated: %s,\n    note: %s,\n    F: [\n%s\n    ],\n    D: %s,\n    G: %s,\n    S: %s' % (
        json.dumps(game, ensure_ascii=False), json.dumps(updated, ensure_ascii=False), json.dumps(note, ensure_ascii=False), rows, js(D), js(G), js(S))
    return html[:m.start(1)] + blk + html[m.end(1):]


def apply_changes(html, cands, today):
    """Returns (new html, report text). Only teams whose lineup passed every check are written."""
    done, skipped, body = [], [], []
    for key, c in cands.items():
        lu, cur, pdt, row = c["lu"], c["cur"], c["date"], c["row"]
        try:
            if row.get("new_numbers"):
                raise ValueError("numbers not on the site roster: %s" % row["new_numbers"])
            if not lu.get("opponent") or lu.get("home_away") not in ("vs", "at"):
                raise ValueError("opponent or home/away not readable")
            cd = parse_game_date(cur["game"])
            if cd and pdt < cd:
                raise ValueError("the post is older than the lines on the site")
            F, D, G, S, flagged = build_lines(lu, cur)
            game = "Last game: <b>%s %s</b> \u00b7 %s" % (lu["home_away"], lu["opponent"], fmt_date(pdt))
            note = "Lines are from the team's lineup post on X for the %s game %s %s." % (fmt_date(pdt)[:-6], lu["home_away"], lu["opponent"])
            cap = {}
            if lu.get("C") is not None:
                cap[str(lu["C"])] = "C"
            for q in lu.get("A") or []:
                cap[str(q)] = "A"
            new_html = write_team(html, key, cur["fmt"], F, D, G, S, game, fmt_date(today), note, cap)
            chk = load_current(new_html, key)
            if nums(chk["F"]) != nums(F) or nums(chk["D"]) != nums(D) or [q[0] for q in chk["G"]] != [q[0] for q in G] or [q[0] for q in chk["S"]] != [q[0] for q in S]:
                raise ValueError("re-reading the written lines did not give the same lines")
            if abs(len(new_html) - len(html)) > 0.01 * len(html):
                raise ValueError("the file changed size more than expected")
            html = new_html
            lines = ["### %s: %s %s, %s" % (key.replace("ncaa_", ""), lu["home_away"], lu["opponent"], fmt_date(pdt)), "Source: %s" % row["post"]]
            lines += diffs(cur["F"], F, "Forwards line") + diffs(cur["D"], D, "Defence pair") + diffs([cur["G"]], [G], "Goalies") + diffs([cur["S"]], [S], "Scratches")
            if flagged:
                lines.append("- Names not already on the site (split from the graphic, please check): " + ", ".join(flagged))
            body.append("\n".join(lines))
            done.append(key)
        except Exception as e:
            skipped.append("- %s: %s" % (key, e))
    text = "Lines read from each team's own lineup post on X by Claude. Merging this updates the site, and the NCAA X poster then posts each changed team.\n\n" + "\n\n".join(body)
    if skipped:
        text += "\n\n### Not changed\n" + "\n".join(skipped)
    return html, done, text


def normalize(lu):
    """Site convention: two goalies in G; any extra goalie is listed with the scratches, which are sorted by number."""
    G = list(lu.get("G") or [])
    if len(G) > 2:
        lu["S"] = sorted(list(lu.get("S") or []) + G[2:], key=lambda q: q[0] if isinstance(q[0], int) else 999)
        lu["G"] = G[:2]
    return lu


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
    ap.add_argument("--apply", action="store_true", help="write the changed lines into index.html (the workflow then opens a pull request)")
    ap.add_argument("--explore", action="store_true", help="only list each team's recent media posts (no Claude)")
    a = ap.parse_args()
    acc = {k: v for k, v in json.load(open(os.path.join(HERE, "ncaa_x_accounts.json"))).items() if not k.startswith("_")}
    want = {x.strip() for x in a.teams.split(",") if x.strip()}
    if want:
        acc = {k: v for k, v in acc.items() if k in want}
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=a.hours)
    print("Looking back %g hours from %s UTC; model %s\n" % (a.hours, since.strftime("%Y-%m-%d %H:%M"), MODEL))
    rep, tin, tout, nimg, cands = [], 0, 0, 0, {}
    for key, cfg in acc.items():
        handle, tx = (cfg, None) if isinstance(cfg, str) else (cfg["handle"], cfg.get("extra"))
        row = {"team": key, "handle": handle, "status": "", "detail": ""}
        rep.append(row)
        try:
            cur = load_current(html, key)
            if not cur:
                row["status"], row["detail"] = "ERROR", "team not found in index.html"
                continue
            posts, stats = recent_photo_posts(handle, since, a.extra or tx or DEFAULT_EXTRA, a.limit)
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
                        try:
                            normalize(lu)
                        except Exception:
                            pass
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
                            row.pop("raw", None)
                            row["status"] = "CHANGED" if diffs else "unchanged"
                            row["detail"] = ("differs in: " + ", ".join(diffs)) if diffs else "matches the site"
                            if new:
                                row["detail"] += "; numbers not on the site roster: %s" % new
                            row["lineup"] = {k: lu.get(k) for k in ("F", "D", "G", "S", "C", "A")}
                            row["current_game"] = cur["game"]
                            if diffs:
                                cands[key] = {"lu": lu, "cur": cur, "date": pdate(p["created"]), "row": row}
                            break
                if row["status"] in ("CHANGED", "unchanged"):
                    break
            if not row["status"]:
                row["status"] = "no lineup found"
                row["detail"] = "read %d photo(s), none was a lineup (saw: %s)" % (seen, "; ".join(str(x) for x in row.get("saw", [])))
        except Exception as e:
            row["status"], row["detail"] = "ERROR", str(e)[:300]
        print("%-24s %-14s %s" % (key, row["status"], row["detail"]))
    print("\nPhotos read: %d, Claude tokens: %d in / %d out" % (nimg, tin, tout))
    if a.apply and cands:
        today = datetime.datetime.now(ET).date()
        new_html, done, text = apply_changes(html, cands, today)
        print("\nApplied to index.html: %s" % (", ".join(done) or "nothing"))
        if done:
            open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8").write(new_html)
            open(os.path.join(HERE, "_pr_body.md"), "w", encoding="utf-8").write(text)
        smp = os.environ.get("GITHUB_STEP_SUMMARY")
        if smp:
            with open(smp, "a") as f:
                f.write("### Pull request text\n\n" + text + "\n\n")
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
