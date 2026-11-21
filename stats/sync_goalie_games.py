#!/usr/bin/env python3
"""Top Six Fantasy: save every goalie's game log (saves and shots against, game by game) to Supabase.

Public points leagues give a goalie +2 for each game with a save percentage above .900, so we need the
real per-game numbers. The GitHub Action runs this just before update_stats.py.

Usage:
    python stats/sync_goalie_games.py          # fetch new games, write them, refresh player_stats.g900
    python stats/sync_goalie_games.py --dry    # fetch and report, write nothing
    FULL=1 python stats/sync_goalie_games.py   # refetch every goalie, not just those with new games

Needs only the Python standard library. Reuses the league feed helpers in update_stats.py.
Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY (same secrets as the stats update).
"""
import collections
import json
import os
import sys
import urllib.error
import urllib.request

import update_stats as U


def sb_base():
    return os.environ["SUPABASE_URL"].rstrip("/")


def stored_counts():
    """player_id -> number of games already saved."""
    counts, start = collections.Counter(), 0
    while True:
        url = sb_base() + "/rest/v1/goalie_games?select=player_id&order=player_id,game_id"
        h = U.sb_headers({"Range-Unit": "items", "Range": "%d-%d" % (start, start + 999)})
        chunk = json.loads(U.http_get(url, h))
        for r in chunk:
            counts[r["player_id"]] += 1
        if len(chunk) < 1000:
            return counts
        start += 1000


def fetch_games(league, season_id, feed_id, our_id):
    d = U.ht(league, feed="statviewfeed", view="player", player_id=feed_id, season_id=season_id,
             site_id="0", league_id="1", statsType="standard")
    out = {}
    for blk in d.get("gameByGame") or []:
        for sec in blk.get("sections") or []:
            for item in sec.get("data") or []:
                r = item.get("row") or {}
                gid = ((item.get("prop") or {}).get("game") or {}).get("gameLink")
                day = r.get("date_played")
                if not gid or not day:
                    continue
                sa, sv, ga = U.to_num(r.get("shots_against")), U.to_num(r.get("saves")), U.to_num(r.get("goals_against"))
                if sa is None and sv is not None and ga is not None:
                    sa = sv + ga
                if sv is None and sa is not None and ga is not None:
                    sv = sa - ga
                if sa is None or sv is None:
                    continue
                out[str(gid)] = {
                    "player_id": our_id, "game_id": str(gid), "played_on": day,
                    "sa": int(sa), "saves": int(sv), "ga": int(ga or 0),
                    "win": str(r.get("win")) == "1", "so": str(r.get("shutout")) == "1",
                    "minutes": r.get("minutes"),
                }
    return list(out.values())


def upsert_games(rows):
    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        req = urllib.request.Request(
            sb_base() + "/rest/v1/goalie_games?on_conflict=player_id,game_id",
            data=json.dumps(batch).encode(),
            headers=U.sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        except urllib.error.HTTPError as e:
            print("ERROR writing goalie games: HTTP %s %s" % (e.code, e.reason))
            print("  database said:", e.read().decode("utf-8", "replace")[:800])
            sys.exit(1)


def refresh_g900():
    req = urllib.request.Request(sb_base() + "/rest/v1/rpc/refresh_goalie_g900", data=b"{}",
                                 headers=U.sb_headers({}), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        print("games over .900 refreshed for", r.read().decode().strip() or "?", "goalies")


def main(dry):
    pool = U.sb_get_players()
    idx = U.build_index(pool)
    matched, leftovers, code_map = {}, [], {}
    for league in U.CLIENT_CODE:
        sid = U.pick_season(league)
        rows = U.fetch_players(league, sid, "goalies")
        print("  %s: %d goalies in the league feed" % (league, len(rows)))
        for r in rows:
            p = U.match(idx, league, r, "G")
            gp = int(U.pick(r, "games_played", "gp") or 0)
            if not p:
                leftovers.append((league, sid, r, gp))
                continue
            code_map.setdefault((league, r.get("team_code")), collections.Counter())[p["team"]] += 1
            matched[p["id"]] = (league, sid, str(r.get("player_id")), gp)
    for league, sid, r, gp in leftovers:
        p = U.fuzzy_match(pool, league, r.get("team_code"), U.row_name(r),
                          str(r.get("jersey_number", "")).strip(), True, code_map, taken=matched)
        if p:
            matched[p["id"]] = (league, sid, str(r.get("player_id")), gp)
    print("matched %d goalies to the player pool" % len(matched))
    have = stored_counts()
    full = bool(os.environ.get("FULL"))
    todo = [(pid, v) for pid, v in matched.items() if full or have.get(pid, 0) != v[3]]
    print("%d goalies have new games to fetch (%d already up to date)" % (len(todo), len(matched) - len(todo)))
    rows, failed = [], 0
    for pid, (league, sid, fid, gp) in todo:
        try:
            rows += fetch_games(league, sid, fid, pid)
        except Exception as e:
            failed += 1
            print("  failed for %s: %s" % (pid, e))
    over = sum(1 for r in rows if r["sa"] > 0 and r["saves"] * 10 > r["sa"] * 9)
    print("fetched %d game rows (%d above .900); %d goalie fetches failed" % (len(rows), over, failed))
    if todo and failed > len(todo) / 2:
        print("ERROR: more than half of the goalie fetches failed; stopping")
        sys.exit(1)
    if dry:
        print("dry run: nothing written")
        return
    if rows:
        upsert_games(rows)
    refresh_g900()
    print("done")


if __name__ == "__main__":
    main("--dry" in sys.argv)
