#!/usr/bin/env python3
"""Posts NCAA - Big Ten lineups to X the same way the CHL ones go out (three screenshots of the team's page).

NCAA lineups are entered by hand in index.html (from each team's own X lineup post), so there is no queue:
this script runs whenever index.html is pushed (see .github/workflows/post-ncaa-x.yml), reads every ncaa_* team
from the page, and posts any team whose "Last game" line is new since the last run. What was already posted is
remembered in lineups/ncaa_x_posted.json, so a team is never posted twice for the same game.

Reuses the CHL poster (post_to_x.py) for the screenshots, the X API calls and the retry/limit handling.
Posts only when the repository variable X_POSTING is "true" and the four X secrets exist; otherwise it just logs.

  python lineups/post_ncaa.py                 normal run (post every team with a new game line)
  python lineups/post_ncaa.py --test KEY      post ONE real test card ("TEST (please ignore)") for e.g. ncaa_michigan
  python lineups/post_ncaa.py --force KEY     post that team's current lineup now, even if it was posted before
"""
import datetime, hashlib, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import post_to_x as X

STATE = os.path.join(X.ROOT, "lineups", "ncaa_x_posted.json")
# Team hashtags for the posts (after #NCAAHockey #BigTen). Edit here to change them.
TAGS = {
    "ncaa_michigan": "GoBlue",
    "ncaa_michiganstate": "GoGreen",
    "ncaa_minnesota": "SkiUMah",
    "ncaa_notredame": "GoIrish",
    "ncaa_ohiostate": "GoBucks",
    "ncaa_pennstate": "WeAre",
    "ncaa_wisconsin": "OnWisconsin",
}
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
READ = """() => Object.keys(L).filter(k => k.indexOf('ncaa_') === 0).map(k => ({
  key: k, name: (C[k] || [k])[0], game: L[k].game || '',
  F: L[k].F || [], D: L[k].D || [], G: L[k].G || []}))"""


def caption(item):
    d = datetime.date.fromisoformat(item["date_only"])
    tags = ["#NCAAHockey", "#BigTen"] + (["#" + TAGS[item["key"]]] if TAGS.get(item["key"]) else [])
    return "\U0001F3D2 %s lines %s %s\n%s %d\n\n%s" % (item["team"], item["vsat"], item["opp"], d.strftime("%b"), d.day, " ".join(tags))


X.caption = caption   # post_item() builds its text with this


def game_id(key, vsat, opp, day):
    # keyed on team + opponent + date, so changing the label text ("Today:" / "Last game:") never causes a repost
    return "ncaa:%s:%s" % (key, hashlib.sha1(("%s|%s|%s" % (vsat, opp, day)).encode("utf-8")).hexdigest()[:8])


def item_from(row):
    m = re.search(r"<b>(vs|at) (.*?)</b>\s*·\s*([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", row["game"])
    if not m or not (row["F"] and row["D"] and row["G"]): return None
    day = datetime.date(int(m.group(5)), MONTHS[m.group(3)], int(m.group(4)))
    return {"id": game_id(row["key"], m.group(1), m.group(2), day.isoformat()), "lg": "ncaa", "key": row["key"], "team": row["name"],
            "opp": m.group(2), "vsat": m.group(1), "date_only": day.isoformat(),
            "start": datetime.datetime.combine(day, datetime.time(19, 0), tzinfo=X.ET).isoformat(),
            "entry": {"F": row["F"], "D": row["D"], "G": row["G"]}}


def main(argv):
    posting = os.environ.get("X_POSTING", "").lower() == "true"
    creds = X.creds_from_env()
    src = open(X.INDEX, encoding="utf-8").read()
    colors, caps = X.load_colors(src), X.load_caps(src)
    import shots
    shooter = shots.Shooter().__enter__()
    try:
        items = [i for i in (item_from(r) for r in shooter.page.evaluate(READ)) if i]
        by_key = {i["key"]: i for i in items}
        state = X.load(STATE, None)
        mode = argv[0] if argv else ""

        if mode in ("--test", "--force"):
            it = by_key.get(argv[1])
            if not it: print("no NCAA lineup for", argv[1]); return 1
            if not creds: print("The four X secrets are not all set."); return 1
            out, info = X.post_item(it, colors, caps.get(it["key"]), creds, prefix="TEST (please ignore) – " if mode == "--test" else "", shooter=shooter)
            print(it["team"], "->", out, info)
            if mode == "--force" and out in ("ok", "dup"):
                state = state or []
                if it["id"] not in state: state.append(it["id"])
                X.save(STATE, state[-500:])
            return 0 if out in ("ok", "dup") else 1

        if state is None:                      # first ever run: remember what is there, post nothing
            X.save(STATE, [i["id"] for i in items]); print("baseline saved for", len(items), "teams"); return 0

        todo = [i for i in items if i["id"] not in state]
        if not todo: print("Nothing new to post"); return 0
        if not posting or not creds:
            print("X posting is OFF (needs X_POSTING=true and the four X secrets). Would have posted:", ", ".join(i["team"] for i in todo))
            return 0
        for it in todo:
            out, info = X.post_item(it, colors, caps.get(it["key"]), creds, shooter=shooter)
            print(it["team"], "->", out, info)
            if out in ("ok", "dup"): state.append(it["id"])
            elif out in ("limit", "fatal"): break          # try again on the next run
            X.time.sleep(X.PAUSE_S)
        X.save(STATE, state[-500:])
        return 0
    finally:
        try: shooter.__exit__(None, None, None)
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
