#!/usr/bin/env python3
"""Top Six Fantasy: copy the players marked injured ("I") on the latest lineup cards into Supabase.

Reads the lineup data (the L object in index.html) and keeps the table pinjuries equal to the set of
players currently flagged "I" in a team's scratch list. The fantasy game only lets a manager move a
player to injured reserve while he is in that table.

Environment: SUPABASE_URL and SUPABASE_SERVICE_KEY (GitHub Actions secrets).
Use --dry to print what would change without writing.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DRY = "--dry" in sys.argv


def rest(method, path, body=None, prefer=None):
    headers = {"apikey": KEY, "Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + "/rest/v1/" + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8")
    return json.loads(text) if text.strip() else None


def injured_from_cards():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    start = html.index("var L = {")
    end = html.index("var INJ", start)
    seg = html[start:end]
    heads = list(re.finditer(r"\n\s*([a-z0-9_]+):\s*\{\s*\n\s*game:", seg))
    teams = {}
    for i, m in enumerate(heads):
        block = seg[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(seg)]
        s = re.search(r"\bS:\s*(\[\s*\[.*?\]\s*\]|\[\s*\])", block, re.S)
        rows = []
        if s:
            try:
                rows = json.loads(s.group(1))
            except ValueError:
                rows = []
        teams[m.group(1)] = [r for r in rows if len(r) > 3 and r[3] == "I"]
    return teams


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def injured_from_report(names):
    """Players on the weekly OHL / WHL injury report (the INJ object in index.html), matched to player ids."""
    out = set()
    try:
        html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        a = html.index("var INJ = ") + len("var INJ = ")
        text = html[a:html.index("\n", a)].strip().rstrip(";")
        report = json.loads(text)
    except (ValueError, OSError):
        return out
    for league in report.values():
        for key, entries in (league.get("t") or {}).items():
            prefix = key + "-"
            cands = [(pid, norm(nm)) for pid, nm in names.items() if pid.startswith(prefix)]
            for e in entries:
                if len(e) > 5 and e[5] and (prefix + str(e[5])) in names:
                    out.add(prefix + str(e[5]))
                    continue
                full = norm(str(e[0]) + " " + str(e[1]))
                hit = [pid for pid, nm in cands if nm == full]
                if not hit:
                    last, first = full.split(" ")[-1], full[:1]
                    hit = [pid for pid, nm in cands if nm.split(" ")[-1:] == [last] and nm[:1] == first]
                if len(hit) == 1:
                    out.add(hit[0])
    return out


def main():
    if not URL or not KEY:
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_KEY are not set")
    teams = injured_from_cards()
    if len(teams) < 20:
        sys.exit("Only found %d teams in the lineup data; leaving injuries unchanged" % len(teams))
    ids = set()
    for key, rows in teams.items():
        for r in rows:
            ids.add("%s-%s" % (key, r[0]))
    known = set()
    names = {}
    offset = 0
    while True:
        page = rest("GET", "players?select=id,name&order=id&limit=1000&offset=%d" % offset)
        known.update(p["id"] for p in page)
        names.update((p["id"], p.get("name") or "") for p in page)
        if len(page) < 1000:
            break
        offset += 1000
    ids |= injured_from_report(names)
    missing = sorted(ids - known)
    ids &= known
    current = set(r["player_id"] for r in rest("GET", "pinjuries?select=player_id&limit=5000"))
    add = sorted(ids - current)
    drop = sorted(current - ids)
    print("injured on cards: %d, in database: %d, adding %d, removing %d, unmatched %d" % (len(ids), len(current), len(add), len(drop), len(missing)))
    if missing:
        print("unmatched:", ", ".join(missing))
    if DRY:
        return
    if add:
        rest("POST", "pinjuries", [{"player_id": p} for p in add], prefer="resolution=merge-duplicates")
    if drop:
        rest("DELETE", "pinjuries?player_id=in.(" + ",".join(urllib.parse.quote(p) for p in drop) + ")")


if __name__ == "__main__":
    main()
