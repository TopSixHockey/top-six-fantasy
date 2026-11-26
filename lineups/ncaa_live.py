#!/usr/bin/env python3
"""Live scores for the NCAA - Big Ten pages.

The page cannot read NCAA.com's public scoreboard feed itself (browsers block it), so this script (run every couple
of minutes during games by .github/workflows/ncaa-live-scores.yml) reads the same public scoreboard data NCAA.com
shows on its scoreboard page, identifies itself honestly, and saves just the Big Ten teams' games of yesterday, today
and tomorrow to lineups/ncaa_live.json. The page reads that file.

The clock is not saved (it would change every minute and mean a commit every minute); the page shows the period.
Exit codes: 0 = games are on or about to start (keep polling), 10 = nothing is happening (stop for now), 1 = feed error.
"""
import datetime, json, os, sys, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "lineups", "ncaa_live.json")
HASH = "4bcb5e6432fa9da365c0c19af01b1f9015cc7eb5c21e7af2dba308784a166df7"   # the scoreboard page's GetContests_web query
OURS = {"michigan", "michigan-st", "minnesota", "notre-dame", "ohio-st", "penn-st", "wisconsin"}


def fetch_day(day, season):
    q = "https://sdataprod.ncaa.com/?" + urllib.parse.urlencode({
        "meta": "GetContests_web",
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": HASH}}, separators=(",", ":")),
        "queryName": "GetContests_web",
        "variables": json.dumps({"sportCode": "MIH", "division": 1, "seasonYear": season,
                                 "contestDate": day.strftime("%m/%d/%Y"), "week": None}, separators=(",", ":"))})
    req = urllib.request.Request(q, headers={"User-Agent": "TopSixHockey-live-scores (topsixhockey.ca)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return (json.loads(r.read()).get("data") or {}).get("contests") or []


def main():
    now = datetime.datetime.now(ET)
    season = now.year if now.month >= 8 else now.year - 1
    games, bad = [], 0
    for off in (-1, 0, 1):
        day = (now + datetime.timedelta(days=off)).date()
        try:
            contests = fetch_day(day, season)
        except Exception as e:
            print("feed error for", day, "-", e); bad += 1; continue
        for c in contests:
            teams = [{"seo": t.get("seoname"), "name": t.get("nameShort"), "score": t.get("score"), "home": bool(t.get("isHome"))}
                     for t in c.get("teams") or []]
            if not any(t["seo"] in OURS for t in teams): continue
            sd = (c.get("startDate") or "").split("/")
            games.append({"id": c.get("contestId"), "date": "%s-%s-%s" % (sd[2], sd[0], sd[1]) if len(sd) == 3 else day.isoformat(),
                          "start": c.get("startTimeEpoch"), "state": c.get("gameState"),
                          "period": c.get("currentPeriod") or "", "teams": teams})
    if bad == 3:
        return 1
    games.sort(key=lambda g: (g["date"], g["start"] or 0, g["id"] or 0))
    try:
        old = json.load(open(OUT, encoding="utf-8")).get("games")
    except Exception:
        old = None
    if old != games:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"updated": now.isoformat(timespec="seconds"), "games": games}, f, ensure_ascii=False, indent=1)
        print("saved", len(games), "games")
    busy = [g for g in games if g["state"] != "F" and g["start"] and g["start"] - 3 * 3600 <= now.timestamp() <= g["start"] + 6 * 3600]
    for g in busy: print("in play/soon:", " vs ".join("%s %s" % (t["name"], t["score"]) for t in g["teams"]), g["state"], g["period"])
    return 0 if busy else 10


if __name__ == "__main__":
    sys.exit(main())
