#!/usr/bin/env python3
"""Top Six Fantasy: pull season stats for OHL / WHL / QMJHL and write them to Supabase.

Usage (run by the GitHub Action, or by hand):
    python stats/update_stats.py discover   # prints what the league feed returns; writes nothing
    python stats/update_stats.py discover-game   # prints what one game's box score contains (GAME_ID, GAME_LEAGUE env)
    python stats/update_stats.py update     # fetches stats and upserts them into player_stats
    python stats/update_stats.py update --dry   # same, but writes nothing

Needs only the Python standard library.
Environment (set as GitHub Actions secrets, never put them in the page):
    SUPABASE_URL           e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY   the service-role key (secret; bypasses row-level security)
Optional overrides if the automatic season pick is wrong:
    SEASON_ID_OHL, SEASON_ID_WHL, SEASON_ID_QMJHL
"""
import collections
import datetime
import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

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
        if ("player_id" in obj or "name" in obj or "shortname" in obj) and ("games_played" in obj or "goals" in obj or "wins" in obj):
            if not isinstance(obj.get("games_played"), dict):  # the column-heading object has dict values
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


# ---------- WHL shots (the league feed leaves them out, but chl.ca's own stats page has them) ----------
def extract_json_array(s, start):
    """Return the JSON array text that begins at s[start] == '[' (string-aware bracket matching)."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    raise ValueError("unterminated array")


def parse_whl_shots_html(html):
    """Find the skater table data embedded in the page and return [(name 'First Last', team_code, shots)]."""
    for m in re.finditer(r"data:\s*\[\[", html):
        before = html[max(0, m.start() - 9000):m.start()]
        titles = re.findall(r'title:\s*"([^"]*)"', before)
        if "Rank" in titles:
            titles = titles[len(titles) - 1 - titles[::-1].index("Rank"):]
        if "SOG" not in titles or "Player" not in titles:
            continue
        i_sog, i_pl, i_tm = titles.index("SOG"), titles.index("Player"), titles.index("Team")
        i_num = titles.index("#") if "#" in titles else None
        data = json.loads(extract_json_array(html, m.end() - 2))
        out = []
        for row in data:
            if len(row) <= i_sog or not isinstance(row[i_pl], list):
                continue
            last_first = row[i_pl][1]
            name = " ".join(reversed([p.strip() for p in last_first.split(",", 1)])) if "," in last_first else last_first
            team = ""
            if isinstance(row[i_tm], list) and row[i_tm] and isinstance(row[i_tm][0], list):
                team = row[i_tm][0][1]
            shots = to_num(row[i_sog])
            if shots is not None:
                num = str(row[i_num]) if i_num is not None and i_num < len(row) else ""
                out.append((name, team, int(shots), num))
        return out
    raise RuntimeError("could not find the skater table data on the WHL stats page")


def fetch_whl_shots(season_id):
    url = "https://chl.ca/whl/stats/players/%s/all/points/" % season_id
    html = http_get(url, {"User-Agent": "Mozilla/5.0 (compatible; TopSixFantasy/1.0; +https://topsixhockey.ca)"}, timeout=90)
    return parse_whl_shots_html(html)


# ---------- turning feed rows into player_stats rows ----------
def power_play_points(r):
    """Power play goals + power play assists (None when the feed has neither)."""
    ppg, ppa = pick(r, "power_play_goals", "ppg"), pick(r, "power_play_assists", "ppa")
    if ppg is None and ppa is None:
        return None
    return (ppg or 0) + (ppa or 0)


def skater_stats(r):
    return {
        "gp": pick(r, "games_played", "gp"),
        "g": pick(r, "goals", "g"),
        "a": pick(r, "assists", "a"),
        "pm": pick(r, "plus_minus", "plusminus"),
        "pim": pick(r, "penalty_minutes", "pim"),
        "ppp": power_play_points(r),
        "shots": pick(r, "shots", "shots_on_goal", "sog"),
    }


def goalie_stats(r):
    svp = pick(r, "save_percentage", "savepct", "sv_pct")
    if svp is not None and svp > 1:  # e.g. 91.2 -> .912
        svp = svp / 100.0
    ga = pick(r, "goals_against")
    sa = pick(r, "shots", "shots_against")
    if sa is None and ga is not None and svp is not None and svp < 1:  # WHL feed has no shots against: work it out from GA and SV%
        sa = float(round(ga / (1.0 - svp)))
    return {
        "gp": pick(r, "games_played", "gp"),
        "wins": pick(r, "wins", "w"),
        "so": pick(r, "shutouts", "so"),
        "gaa": pick(r, "goals_against_average", "gaa"),
        "svp": svp,
        "sa": sa,
        "ga": ga,
    }


def split_name(full):
    parts = (full or "").replace(".", ". ").split()
    if len(parts) < 2:
        return None
    return norm_name(parts[0])[:1], norm_name("".join(parts[1:]))


def build_index(pool):
    """Maps (league, normalized full name) and (league, initial, surname) -> [players]."""
    idx = {}
    for p in pool:
        idx.setdefault((p["league"], norm_name(p["name"])), []).append(p)
        sn = split_name(p["name"])
        if sn:
            idx.setdefault((p["league"], "init", sn[0], sn[1]), []).append(p)
    return idx


def row_name(row):
    name = pick_text(row, "name", "player_name", "shortname")
    if not name:
        name = (pick_text(row, "first_name") + " " + pick_text(row, "last_name")).strip()
    if "," in name:  # QMJHL: "Veilleux, Philippe" -> "Philippe Veilleux"
        last, first = name.split(",", 1)
        name = (first.strip() + " " + last.strip()).strip()
    return name


def match(idx, league, row, want_group=None):
    name = row_name(row)
    cands = idx.get((league, norm_name(name)), [])
    if not cands and re.match(r"^[A-Za-z]\.?\s", name):  # "J. Smith" style
        sn = split_name(name)
        if sn:
            cands = idx.get((league, "init", sn[0], sn[1]), [])
    if want_group:
        cands = [c for c in cands if c["pos_group"] == want_group]
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        jersey = str(row.get("jersey_number", "")).strip()
        if jersey:
            by_num = [c for c in cands if c.get("num") is not None and str(c["num"]) == jersey]
            if len(by_num) == 1:
                return by_num[0]
            if by_num:
                cands = by_num
        hint = norm_name(pick_text(row, "team_name", "team_city", "city", "team_code"))
        if hint:
            narrowed = [c for c in cands if hint in norm_name(c["team"]) or norm_name(c["team"]) in hint]
            if len(narrowed) == 1:
                return narrowed[0]
    return None


def fuzzy_match(pool, league, team_code, name, jersey, goalie, code_map, taken=None):
    """Second pass for spelling differences. Only accepts a very close name on the SAME team
    (team found from the feed's team code via players already matched exactly)."""
    if not name or re.match(r"^[A-Za-z]\.?\s", name):
        return None
    teams = code_map.get((league, team_code))
    if not teams:
        return None
    team = max(teams, key=teams.get)
    target = norm_name(name)
    scored = []
    for c in pool:
        if c["league"] != league or c["team"] != team or (c["pos_group"] == "G") != goalie:
            continue
        if taken is not None and c["id"] in taken:
            continue
        r = difflib.SequenceMatcher(None, target, norm_name(c["name"])).ratio()
        same_num = jersey != "" and str(c.get("num")) == str(jersey)
        if same_num and r >= 0.7:   # same team + same jersey number + similar name
            scored.append((r, c))
    scored.sort(key=lambda x: -x[0])
    if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        return scored[0][1]
    return None


def clean(d):
    """Drop None so we never overwrite an existing number with null; make whole numbers ints."""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        out[k] = int(v) if k in ("gp", "g", "a", "pm", "pim", "ppp", "shots", "wins", "so", "sa", "ga") else v
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
        url = base + "/rest/v1/players?select=id,league,name,team,pos_group,num&order=id"
        h = sb_headers({"Range-Unit": "items", "Range": "%d-%d" % (start, start + 999)})
        chunk = json.loads(http_get(url, h))
        out += chunk
        if len(chunk) < 1000:
            return out
        start += 1000


def sb_snapshot():
    """After a stats update, record today's season fantasy points per player (powers the monthly leaders)."""
    base = os.environ["SUPABASE_URL"].rstrip("/")
    req = urllib.request.Request(base + "/rest/v1/rpc/snapshot_points", data=b"{}",
                                 headers=sb_headers({}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print("Snapshot saved for", r.read().decode().strip() or "?", "players")
    except urllib.error.HTTPError as e:
        print("Snapshot skipped: HTTP %s %s %s" % (e.code, e.reason, e.read().decode("utf-8", "replace")[:300]))
    except Exception as e:
        print("Snapshot skipped:", e)


def sb_upsert_stats(rows):
    """Upsert in batches of rows that all have the same columns (so no column is ever blanked by accident).
    On failure, print the database's own error message."""
    base = os.environ["SUPABASE_URL"].rstrip("/")
    groups = {}
    for r in rows:
        groups.setdefault(tuple(sorted(r.keys())), []).append(r)
    for keys, grp in groups.items():
        for i in range(0, len(grp), 300):
            batch = grp[i:i + 300]
            req = urllib.request.Request(
                base + "/rest/v1/player_stats?on_conflict=player_id",
                data=json.dumps(batch).encode(),
                headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    r.read()
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:800]
                print("ERROR writing to Supabase: HTTP %s %s" % (e.code, e.reason))
                print("  columns in this batch:", ", ".join(keys))
                print("  database said:", body)
                print("  first row of batch:", json.dumps(batch[0]))
                sys.exit(1)


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


def cmd_discover_game():
    """Print what the per-game box-score feed contains (looking for per-player shots)."""
    gid = os.environ.get("GAME_ID", "1023302")
    league = os.environ.get("GAME_LEAGUE", "WHL")
    for tab in ("gamesummary", "boxscore"):
        print("=" * 70)
        print("%s game %s, tab=%s" % (league, gid, tab))
        try:
            data = ht(league, feed="gc", tab=tab, game_id=gid)
        except Exception as e:
            print("  failed:", e)
            continue
        if isinstance(data, dict):
            print("  top-level keys:", sorted(data.keys()))
        found = []

        def walk(o, path):
            if isinstance(o, dict):
                if any(isinstance(k, str) and ("shot" in k.lower() or k.lower() == "sog") for k in o):
                    keep = ("shot", "sog", "goal", "assist", "first_name", "last_name", "player_id", "name", "jersey")
                    found.append((path, {k: o[k] for k in o if isinstance(k, str) and any(w in k.lower() for w in keep)}))
                for k, v in o.items():
                    walk(v, path + "/" + str(k))
            elif isinstance(o, list):
                for i, v in enumerate(o[:60]):
                    walk(v, path + "[" + str(i) + "]")

        walk(data, "")
        print("  places where a 'shots' field appears: %d" % len(found))
        for path, vals in found[:8]:
            print("   ", path, json.dumps(vals)[:300])
        if not found:
            print("  raw start:", json.dumps(data)[:900])
    print("=" * 70)


def cmd_update(dry):
    pool = sb_get_players()
    print("pool: %d players" % len(pool))
    idx = build_index(pool)
    group = {p["id"]: p["pos_group"] for p in pool}
    out, unmatched, shots_seen = {}, {}, 0
    pairs, leftovers, code_map = [], [], {}

    def record(p, r, conv):
        nonlocal shots_seen
        s = clean(conv(r))
        if "shots" in s:
            shots_seen += 1
        if s:
            s["player_id"] = p["id"]
            out[p["id"]] = s

    for league in CLIENT_CODE:
        print(league)
        sid = pick_season(league)
        for pos, conv in (("skaters", skater_stats), ("goalies", goalie_stats)):
            rows = fetch_players(league, sid, pos)
            print("  %s: %d rows from the league feed" % (pos, len(rows)))
            for r in rows:
                p = match(idx, league, r, "G" if pos == "goalies" else None)
                if not p:
                    leftovers.append((league, pos, r, conv))
                    continue
                if (group[p["id"]] == "G") != (pos == "goalies"):
                    continue
                code_map.setdefault((league, r.get("team_code")), collections.Counter())[p["team"]] += 1
                record(p, r, conv)
    # second pass: spelling differences, same team only
    for league, pos, r, conv in leftovers:
        p = fuzzy_match(pool, league, r.get("team_code"), row_name(r), str(r.get("jersey_number", "")).strip(),
                        pos == "goalies", code_map, taken=out)
        if p:
            print("  fuzzy match: '%s' -> '%s' (%s)" % (row_name(r), p["name"], p["team"]))
            record(p, r, conv)
        else:
            unmatched.setdefault(league, []).append(row_name(r))
    # WHL: add shots from chl.ca's stats page. If this fails we keep yesterday's shots and carry on.
    try:
        whl_sid = pick_season("WHL")
        added = missed = 0
        for name, team, shots, num in fetch_whl_shots(whl_sid):
            p = match(idx, "WHL", {"name": name, "team_code": team, "jersey_number": num}, None)
            if not p:
                p = fuzzy_match(pool, "WHL", team, name, num, False, code_map)
            if p and p["pos_group"] != "G" and p["id"] in out:
                out[p["id"]]["shots"] = shots
                added += 1
            else:
                missed += 1
        shots_seen += added
        print("WHL shots from chl.ca: added for %d skaters (%d page rows not in the pool)" % (added, missed))
    except Exception as e:
        print("WARNING: WHL shots step failed (%s); WHL shots left as they were" % e)
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for row in out.values():
        row["updated_at"] = stamp
    print("matched %d of %d pool players; %d skater rows had shots" % (len(out), len(pool), shots_seen))
    by_lg = {}
    for p in pool:
        by_lg.setdefault(p["league"], [0, 0])[0] += 1
        by_lg[p["league"]][1] += p["id"] in out
    for lg, (tot, hit) in by_lg.items():
        print("  %s: matched %d of %d pool players" % (lg, hit, tot))
    missing = [p["name"] + " (" + p["team"] + ")" for p in pool if p["id"] not in out]
    print("  pool players with no stats row (first 15):", ", ".join(missing[:15]))
    for lg, names in unmatched.items():
        print("  %s: %d feed players not in the pool (call-ups, etc.); examples: %s" % (lg, len(names), ", ".join(names[:6])))
    if len(out) < 0.5 * len(pool):
        print("ERROR: fewer than half the pool matched; refusing to write. Run 'discover' and check field names.")
        sys.exit(1)
    if dry:
        print("dry run: nothing written")
        return
    sb_upsert_stats(list(out.values()))
    # Only the overnight (3 AM Eastern) run saves the daily snapshot: it is the start-of-day baseline the leagues
    # score weeks from. The 5 PM and 10 PM runs just refresh player_stats; league scores read those live.
    hour = datetime.datetime.now(ZoneInfo("America/Toronto")).hour
    if hour < 9 or os.environ.get("FORCE_SNAPSHOT"):
        sb_snapshot()
    else:
        print("Snapshot skipped: only the overnight run saves the daily snapshot (Eastern hour %d)" % hour)
    print("done: wrote %d stat rows" % len(out))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "update"
    if mode == "discover":
        cmd_discover()
    elif mode == "discover-game":
        cmd_discover_game()
    elif mode == "update":
        cmd_update("--dry" in sys.argv)
    else:
        print(__doc__)
        sys.exit(2)
