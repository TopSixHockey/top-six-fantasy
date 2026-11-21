#!/usr/bin/env python3
"""Top Six Fantasy: save every player's game log (game by game) to Supabase.

Goalies: saves and shots against per game (public points leagues give +2 for each game above .900).
Skaters: goals, assists, +/-, PIM and shots per game (powers the last 14 / 30 day views).
The GitHub Action runs this just before update_stats.py.

Usage:
    python stats/sync_goalie_games.py          # fetch new games, write them, refresh player_stats.g900
    python stats/sync_goalie_games.py --dry    # fetch and report, write nothing
    FULL=1 python stats/sync_goalie_games.py   # refetch every player, not just those with new games

Needs only the Python standard library. Reuses the league feed helpers in update_stats.py.
Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY (same secrets as the stats update).
"""
import collections
import concurrent.futures
import json
import os
import sys
import urllib.error
import urllib.request

import update_stats as U


def sb_base():
    return os.environ["SUPABASE_URL"].rstrip("/")


def stored_counts():
    """player_id -> number of games already saved (skaters and goalies)."""
    req = urllib.request.Request(sb_base() + "/rest/v1/rpc/game_log_counts", data=b"{}",
                                 headers=U.sb_headers({}), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def log_rows(league, season_id, feed_id):
    d = U.ht(league, feed="statviewfeed", view="player", player_id=feed_id, season_id=season_id,
             site_id="0", league_id="1", statsType="standard")
    for blk in d.get("gameByGame") or []:
        for sec in blk.get("sections") or []:
            for item in sec.get("data") or []:
                r = item.get("row") or {}
                gid = ((item.get("prop") or {}).get("game") or {}).get("gameLink")
                day = r.get("date_played")
                if gid and day:
                    yield str(gid), day, r


def goalie_games(league, season_id, feed_id, our_id):
    out = {}
    for gid, day, r in log_rows(league, season_id, feed_id):
        sa, sv, ga = U.to_num(r.get("shots_against")), U.to_num(r.get("saves")), U.to_num(r.get("goals_against"))
        if sa is None and sv is not None and ga is not None:
            sa = sv + ga
        if sv is None and sa is not None and ga is not None:
            sv = sa - ga
        if sa is None or sv is None:
            continue
        out[gid] = {
            "player_id": our_id, "game_id": gid, "played_on": day,
            "sa": int(sa), "saves": int(sv), "ga": int(ga or 0),
            "win": str(r.get("win")) == "1", "so": str(r.get("shutout")) == "1",
            "minutes": r.get("minutes"),
        }
    return list(out.values())


def skater_games(league, season_id, feed_id, our_id):
    out = {}
    for gid, day, r in log_rows(league, season_id, feed_id):
        shots = U.to_num(r.get("shots"))
        pmv = U.to_num(r.get("plusminus"))   # the QMJHL game log has no +/-: store null, not 0
        out[gid] = {
            "player_id": our_id, "game_id": gid, "played_on": day,
            "g": int(U.to_num(r.get("goals")) or 0), "a": int(U.to_num(r.get("assists")) or 0),
            "pm": int(pmv) if pmv is not None else None, "pim": int(U.to_num(r.get("penalty_minutes")) or 0),
            "shots": int(shots) if shots is not None else None,
        }
    return list(out.values())


def upsert(table, rows):
    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        req = urllib.request.Request(
            sb_base() + "/rest/v1/%s?on_conflict=player_id,game_id" % table,
            data=json.dumps(batch).encode(),
            headers=U.sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        except urllib.error.HTTPError as e:
            print("ERROR writing %s: HTTP %s %s" % (table, e.code, e.reason))
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
        for pos in ("skaters", "goalies"):
            rows = U.fetch_players(league, sid, pos)
            print("  %s %s: %d in the league feed" % (league, pos, len(rows)))
            for r in rows:
                p = U.match(idx, league, r, "G" if pos == "goalies" else None)
                gp = int(U.pick(r, "games_played", "gp") or 0)
                if not p:
                    leftovers.append((league, sid, pos, r, gp))
                    continue
                if (p["pos_group"] == "G") != (pos == "goalies"):
                    continue
                code_map.setdefault((league, r.get("team_code")), collections.Counter())[p["team"]] += 1
                matched[p["id"]] = (league, sid, str(r.get("player_id")), gp, pos == "goalies")
    for league, sid, pos, r, gp in leftovers:
        p = U.fuzzy_match(pool, league, r.get("team_code"), U.row_name(r),
                          str(r.get("jersey_number", "")).strip(), pos == "goalies", code_map, taken=matched)
        if p:
            matched[p["id"]] = (league, sid, str(r.get("player_id")), gp, pos == "goalies")
    print("matched %d players to the player pool" % len(matched))
    have = stored_counts()
    full = bool(os.environ.get("FULL"))
    todo = [(pid, v) for pid, v in matched.items() if v[3] > 0 and (full or have.get(pid, 0) != v[3])]
    print("%d players have new games to fetch (%d already up to date)" % (len(todo), len(matched) - len(todo)))

    def work(item):
        pid, (league, sid, fid, gp, is_g) = item
        return is_g, (goalie_games if is_g else skater_games)(league, sid, fid, pid)

    g_rows, s_rows, failed = [], [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, it): it for it in todo}
        for f in concurrent.futures.as_completed(futs):
            try:
                is_g, rows = f.result()
                (g_rows if is_g else s_rows).extend(rows)
            except Exception as e:
                failed += 1
                print("  failed for %s: %s" % (futs[f][0], e))
    over = sum(1 for r in g_rows if r["sa"] > 0 and r["saves"] * 10 > r["sa"] * 9)
    print("fetched %d goalie games (%d above .900) and %d skater games; %d fetches failed" % (len(g_rows), over, len(s_rows), failed))
    if todo and failed > len(todo) / 2:
        print("ERROR: more than half of the fetches failed; stopping")
        sys.exit(1)
    if dry:
        print("dry run: nothing written")
        return
    if g_rows:
        upsert("goalie_games", g_rows)
    if s_rows:
        upsert("skater_games", s_rows)
    refresh_g900()
    print("done")


if __name__ == "__main__":
    main("--dry" in sys.argv)
