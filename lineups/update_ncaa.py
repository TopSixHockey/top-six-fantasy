#!/usr/bin/env python3
"""Adds an "NCAA" line to player pop-ups for players with an NCAA commitment.

Reads lineups/ncaa_commitments.json (a list of {"name", "school", "year", optional "team" key and "dob"}) and sets or
removes the "ncaa" field on each matching player in the PL object in index.html. Matching is by name inside the
lineup players; add "dob" (YYYY-MM-DD) to tell apart two players with the same name.

  python lineups/update_ncaa.py          apply the file
  python lineups/update_ncaa.py --dry    show what would change

Safe by design: a missing or unreadable file changes nothing.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_lineups as U
import update_players as P

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ncaa_commitments.json")


def main():
    dry = "--dry" in sys.argv
    try:
        rows = json.load(open(FILE, encoding="utf-8"))
    except Exception as e:
        print("ncaa: no usable commitments file, skipped:", e)
        return
    want = {}
    for r in rows:
        if r.get("name") and r.get("school"):
            line = r["school"] + (" · " + r["year"] if r.get("year") else "")
            want.setdefault(P.norm(r["name"]), []).append((r, line))
    src = open(U.INDEX, encoding="utf-8").read()
    i = src.index("var PL = ") + len("var PL = ")
    PL, n = json.JSONDecoder().raw_decode(src[i:])
    end = i + n
    lineup = P.lineup_players(src)
    set_n = cleared = 0
    for key, players in lineup.items():
        for (num, first, last) in players:
            e = PL.get(key, {}).get(str(num))
            if e is None:
                continue
            hits = want.get(P.norm(first + " " + last), [])
            hits = [(r, l) for r, l in hits if (not r.get("dob") or r["dob"] == e.get("dob")) and (not r.get("team") or r["team"] == key)]
            new = hits[0][1] if hits else None
            if new and e.get("ncaa") != new:
                e["ncaa"] = new
                set_n += 1
                print("  NCAA:", key, num, first, last, "->", new)
            elif not new and "ncaa" in e:
                del e["ncaa"]
                cleared += 1
                print("  NCAA removed:", key, num, first, last)
    print("ncaa: set/updated %d, removed %d" % (set_n, cleared))
    if dry:
        return
    out = src[:i] + U.js(PL) + src[end:]
    if out != src:
        open(U.INDEX, "w", encoding="utf-8").write(out)


if __name__ == "__main__":
    main()
