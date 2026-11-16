#!/usr/bin/env python3
"""Top Six Fantasy: pull season stats for OHL / WHL / QMJHL and write them to Supabase.

Usage (run by the GitHub Action, or by hand):
    python stats/update_stats.py discover   # prints what the league feed returns; writes nothing
    python stats/update_stats.py update     # fetches stats and upserts them into player_stats
    python stats/update_stats.py update --dry   # same, but writes nothing

Needs only the Python standard library.
Environment (set as GitHub Actions secrets, never put them in the page):
    SUPABASE_URL           e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY   the service-role key (secret; bypasses row-level security)
Optional overrides if the automatic season pick is wrong:
    SEASON_ID_OHL, SEASON_ID_WHL, SEASON_ID_QMJHL
"""
import datetime
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

# Public client key that the league websites themselves use for their stats widgets.
HT_KEY = "f1aa699db3d81487"
HT_BASE = "https://lscluster.hockeytech.com/feed/index.php"
CLIENT_CODE = {"OHL": "ohl", "WHL": "whl", "QMJHL": "lhjmq"}


# ---------- small helpers ----------
def http_get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "TopSixFantasy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse_json_loose(text):
    """The feed sometimes wraps JSON in parentheses (JSONP style). Strip that, then parse."""
    t = text.strip()
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1]
    return json.loads(t)


def ht(league, **params):
    q = {"key": HT_KEY, "client_code": CLIENT_CODE[league], "fmt": "json", "lang": "en"}
    q.update(params)
    return parse_json_loose(http_get(HT_BASE + "?" + urllib.parse.urlencode(q)))


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())


def to_num(v):
    if v is None:
        return None
    v = str(v).strip()
    if v in ("", "-", "--", "N/A"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def pick(row, *keys):
    for k in keys:
        if k in row and to_num(row[k]) is not None:
            return to_num(row[k])
    return None


def pick_text(row, *keys):
    for k in keys:
        if k in row and isinstance(row[k], str) and row[k].strip():
            return row[k].strip()
    return ""


def find_rows(obj, out=None):
    """Collect player rows from whatever nesting the feed uses."""
    if out is None:
        out = []
    if isinstance(obj, dict):
        if isinstance(obj.get("row"), dict):
            out.append(obj["row"])
            return out
        if ("player_id" in obj or "name" in obj) and ("games_played" in obj or "goals" in obj or "wins" in obj):
            out.append(obj)
            return out
        for v in obj.values():
            find_rows(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_rows(v, out)
    return out


# ---------- fetching ----------
def pick_season(league):
    override = os.environ.get("SEASON_ID_" + league)
    if override:
        return override
    data = ht(league, feed="modulekit", view="seasons")
    seasons = []

    def walk(o):
        if isinstance(o, dict):
            if "season_id" in o and "season_name" in o:
                seasons.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    regular = [
        s for s in seasons
        if str(s.get("playoff", "0")) == "0"
        and "regular" in s["season_name"].lower()
        and "pre" not in s["season_name"].lower()
    ]
    if not regular:
        raise RuntimeError("no regular season found for %s; set SEASON_ID_%s" % (league, league))
    best = max(regular, key=lambda s: int(s["season_id"]))
    print("  %s season: %s (id %s)" % (league, best["season_name"], best["season_id"]))
    return str(best["season_id"])


def fetch_players(league, season_id, position, limit=2000):
    data = ht(
        league, feed="statviewfeed", view="players", season=season_id, team="all",
        position=position, rookie="0", statsType="standard", rookieOnly="0",
        league_id="1", limit=str(limit), sort="points" if position == "skaters" else "wins",
        division="-1", conference="-1", site_id="0",
    )
    return find_rows(data)


# ---------- turning feed rows into player_stats rows ----------
def skater_stats(r):
    return {
        "gp": pick(r, "games_played", "gp"),
        "g": pick(r, "goals", "g"),
        "a": pick(r, "assists", "a"),
        "pm": pick(r, "plus_minus", "plusminus"),
        "pim": pick(r, "penalty_minutes", "pim"),
        "shots": pick(r, "shots", "shots_on_goal", "sog"),
    }


def goalie_stats(r):
    svp = pick(r, "save_percentage", "savepct", "sv_pct")
    if svp is not None and svp > 1:  # e.g. 91.2 -> .912
        svp = svp / 100.0
    return {
        "gp": pick(r, "games_played", "gp"),
        "wins": pick(r, "wins", "w"),
        "so": pick(r, "shutouts", "so"),
        "gaa": pick(r, "goals_against_average", "gaa"),
        "svp": svp,
    }


def build_index(pool):
    """pool: list of dicts with id, league, name, team. Maps (league, normalized name) -> [players]."""
    idx = {}
    for p in pool:
        idx.setdefault((p["league"], norm_name(p["name"])), []).append(p)
    return idx


def match(idx, league, row):
    name = pick_text(row, "name", "player_name")
    if not name:
        first = pick_text(row, "first_name")
        last = pick_text(row, "last_name")
        name = (first + " " + last).strip()
    cands = idx.get((league, norm_name(name)), [])
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        hint = norm_name(pick_text(row, "team_name", "team_city", "city", "team_code"))
        if hint:
            narrowed = [c for c in cands if hint in norm_name(c["team"]) or norm_name(c["team"]) in hint]
            if len(narrowed) == 1:
                return narrowed[0]
    return None


def clean(d):
    """Drop None so we never overwrite an existing number with null; make whole numbers ints."""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        out[k] = int(v) if k in ("gp", "g", "a", "pm", "pim", "shots", "wins", "so") else v
    return out


# ---------- Supabase ----------
def sb_headers(extra=None):
    key = os.environ["SUPABASE_SERVICE_KEY"]
    h = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):  # legacy service_role keys are JWTs and also go in Authorization
        h["Authorization"] = "Bearer " + key
    if extra:
        h.update(extra)
    return h


def sb_get_players():
    base = os.environ["SUPABASE_URL"].rstrip("/")
    out, start = [], 0
    while True:
        url = base + "/rest/v1/players?select=id,league,name,team,pos_group&order=id"
        h = sb_headers({"Range-Unit": "items", "Range": "%d-%d" % (start, start + 999)})
        chunk = json.loads(http_get(url, h))
        out += chunk
        if len(chunk) < 1000:
            return out
        start += 1000


def sb_upsert_stats(rows):
    base = os.environ["SUPABASE_URL"].rstrip("/")
    for i in range(0, len(rows), 400):
        batch = rows[i:i + 400]
        req = urllib.request.Request(
            base + "/rest/v1/player_stats?on_conflict=player_id",
            data=json.dumps(batch).encode(),
            headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()


# ---------- commands ----------
def cmd_discover():
    for league in CLIENT_CODE:
        print("=" * 70)
        print(league)
        try:
            sid = pick_season(league)
        except Exception as e:  # keep going so we see every league
            print("  season lookup failed:", e)
            continue
        for pos in ("skaters", "goalies"):
            try:
                rows = fetch_players(league, sid, pos)
            except Exception as e:
                print("  %s fetch failed: %s" % (pos, e))
                continue
            print("  %s: %d rows" % (pos, len(rows)))
            if rows:
                print("  field names:", sorted(rows[0].keys()))
                print("  first row  :", json.dumps(rows[0])[:600])
    print("=" * 70)


def cmd_update(dry):
    pool = sb_get_players()
    print("pool: %d players" % len(pool))
    idx = build_index(pool)
    group = {p["id"]: p["pos_group"] for p in pool}
    out, unmatched, shots_seen = {}, {}, 0
    for league in CLIENT_CODE:
        print(league)
        sid = pick_season(league)
        for pos, conv in (("skaters", skater_stats), ("goalies", goalie_stats)):
            rows = fetch_players(league, sid, pos)
            print("  %s: %d rows from the league feed" % (pos, len(rows)))
            for r in rows:
                p = match(idx, league, r)
                if not p:
                    unmatched.setdefault(league, []).append(pick_text(r, "name", "player_name"))
                    continue
                want_goalie = pos == "goalies"
                if (group[p["id"]] == "G") != want_goalie:
                    continue
                s = clean(conv(r))
                if "shots" in s:
                    shots_seen += 1
                if s:
                    s["player_id"] = p["id"]
                    out[p["id"]] = s
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for row in out.values():
        row["updated_at"] = stamp
    print("matched %d of %d pool players; %d skater rows had shots" % (len(out), len(pool), shots_seen))
    for lg, names in unmatched.items():
        print("  %s: %d feed players not in the pool (call-ups, etc.)" % (lg, len(names)))
    if len(out) < 0.5 * len(pool):
        print("ERROR: fewer than half the pool matched; refusing to write. Run 'discover' and check field names.")
        sys.exit(1)
    if dry:
        print("dry run: nothing written")
        return
    sb_upsert_stats(list(out.values()))
    print("done: wrote %d stat rows" % len(out))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "update"
    if mode == "discover":
        cmd_discover()
    elif mode == "update":
        cmd_update("--dry" in sys.argv)
    else:
        print(__doc__)
        sys.exit(2)
