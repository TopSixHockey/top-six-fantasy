u#!/usr/bin/env python3
"""Keeps the player pop-ups on the lineup page (the PL object in index.html) complete and current.

  python lineups/update_players.py          automatic: adds a pop-up for any player in a lineup who lacks one,
                                            and refreshes every player's stats three times a day (3 AM, 5 PM, 10 PM ET)
  python lineups/update_players.py --full   refresh everything now
  add --dry to either to print what would change without writing anything

Player details (birthday, size, hometown, draft line) and season stats come from the same HockeyTech league
feed the lineup bot already uses.
"""
import datetime, json, os, re, sys, unicodedata
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_lineups as U

LG_KEYS = {"ohl": U.OHL_KEYS, "whl": U.WHL_KEYS, "qmjhl": U.QMJHL_KEYS}
LABEL = {"ohl": "OHL Draft", "whl": "WHL Draft", "qmjhl": "QMJHL Draft"}
SHORT = {"ohl": "OHL", "whl": "WHL", "qmjhl": "QMJHL"}
SLOTS = (3, 17, 22)   # Eastern hours of the full stat refreshes
EXTRA = {"ohl": {"MISS": "Mississauga Steelheads", "HAM": "Hamilton Bulldogs"}}   # teams that were renamed or moved
NHL = {
    "ANA": "Anaheim Ducks", "ARI": "Arizona Coyotes", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes", "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche",
    "CBJ": "Columbus Blue Jackets", "DAL": "Dallas Stars", "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers", "LA": "Los Angeles Kings", "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens", "NSH": "Nashville Predators", "NAS": "Nashville Predators",
    "NJ": "New Jersey Devils", "NJD": "New Jersey Devils", "NYI": "New York Islanders", "NYR": "New York Rangers",
    "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins", "SJ": "San Jose Sharks",
    "SJS": "San Jose Sharks", "SEA": "Seattle Kraken", "STL": "St. Louis Blues", "TB": "Tampa Bay Lightning",
    "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs", "UTA": "Utah Mammoth", "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights", "VEG": "Vegas Golden Knights", "WSH": "Washington Capitals",
    "WAS": "Washington Capitals", "WPG": "Winnipeg Jets",
}
PLAYER = re.compile(r'\[(\d+),\s*("(?:[^"\\]|\\.)*"),\s*("(?:[^"\\]|\\.)*")\]')


def norm(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    return re.sub(r"[^a-z]", "", s.encode("ascii", "ignore").decode().lower())


def num(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def clean_name(n):
    return str(n or "").replace(", ", " ").replace("67's", "67’s")


def fmt_ht(h):
    h = str(h or "").strip()
    m = re.match(r"^(\d+)[.'\-](\d+)", h)
    return "%d'%d\"" % (int(m.group(1)), int(m.group(2))) if m else h


def stat_list(goalie, r):
    if goalie:
        if not r or num(r.get("games_played")) == 0:
            return [0, "-", "-", 0, 0, 0, 0]
        gaa, sv = r.get("goals_against_average"), r.get("save_percentage")
        try:
            gaa_s = "%.2f" % float(gaa)
        except (TypeError, ValueError):
            gaa_s = "-"
        try:
            sv = float(sv)
            sv_s = ("%.3f" % (sv / 100.0 if sv > 1 else sv)).lstrip("0")
        except (TypeError, ValueError):
            sv_s = "-"
        return [num(r.get("games_played")), gaa_s, sv_s, num(r.get("wins")), num(r.get("losses")),
                num(r.get("ot_losses")), num(r.get("shutouts"))]
    if not r:
        return [0, 0, 0, 0, 0, 0]
    return [num(r.get(k)) for k in ("games_played", "goals", "assists", "points", "plus_minus", "penalty_minutes")]


# ---------------------------------------------------------------- feed
def walk_rows(o, out):
    if isinstance(o, list):
        for x in o:
            walk_rows(x, out)
    elif isinstance(o, dict):
        if isinstance(o.get("row"), dict):
            out.append(o["row"])
        for v in o.values():
            walk_rows(v, out)


def fetch_rows(lg, pos):
    cfg = U.LEAGUES[lg]
    url = (f"{U.FEED}?feed=statviewfeed&view=players&season={cfg['season']}&team=all&position={pos}&rookie=0"
           f"&statsType=standard&rookieOnly=0&league_id={cfg['league_id']}&limit=3000"
           f"&sort={'points' if pos == 'skaters' else 'wins'}&division=-1&conference=-1&site_id=0"
           f"&key={U.KEY}&client_code={cfg['client']}&lang=en&fmt=json")
    t = U.get(url).strip()
    if t.startswith("("):
        t = t[1:-1]
    out = []
    walk_rows(json.loads(t), out)
    return out


def load_codes():
    codes = {}
    for lg, cfg in U.LEAGUES.items():
        try:
            teams = U.feed(cfg["client"], "teamsbyseason", season_id=cfg["season"])["Teamsbyseason"]
        except Exception as e:
            print(lg, "team list failed:", e)
            teams = []
        codes[lg] = {str(t.get("team_code") or t.get("code") or ""): clean_name(t["name"]) for t in teams}
        codes[lg + "_teams"] = teams
    return codes


def load_league(lg, codes, want_keys):
    """-> (rosters by page key, stats by player id) or None if the feed looks wrong."""
    cfg = U.LEAGUES[lg]
    rosters = {}
    for t in codes[lg + "_teams"]:
        key = U.team_key(t["name"], lg)
        if not key or (want_keys is not None and key not in want_keys):
            continue
        ros = U.feed(cfg["client"], "roster", season_id=cfg["season"], team_id=t["id"]).get("Roster") or []
        for p in ros:
            p["_team"] = clean_name(t["name"])
        rosters.setdefault(key, []).extend(ros)
    stats = {}
    for pos in ("skaters", "goalies"):
        rows = fetch_rows(lg, pos)
        if len(rows) < 20:
            print(lg, pos, "stats look incomplete (%d rows), skipping this league" % len(rows))
            return None
        stats[pos] = {str(r["player_id"]): r for r in rows if r.get("player_id")}
    return rosters, stats


def team_name(code, lg, codes):
    code = str(code or "").strip()
    if not code:
        return ""
    if code in codes[lg]:
        return codes[lg][code]
    if code in EXTRA.get(lg, {}):
        return EXTRA[lg][code]
    for other in LG_KEYS:
        if other != lg and code in codes[other]:
            return "%s (%s)" % (codes[other][code], SHORT[other])
    for n in codes[lg].values():
        if len(code) >= 4 and code.lower() in n.lower():
            return n
    return code


# ---------------------------------------------------------------- building entries
def nhl_line(p):
    for d in p.get("draftinfo") or []:
        if d.get("draft_type") == "NHL":
            code = str(d.get("draft_team") or "")
            return "%s · Round %s, #%s · %s" % (d.get("draft_year"), d.get("draft_round"), d.get("draft_rank"),
                                                          NHL.get(code.upper(), code))
    return None


def build_entry(p, lg, codes, eligible):
    pos = {"LD": "D", "RD": "D"}.get(p.get("position"), p.get("position") or "F")
    home = p.get("homeplace") or ", ".join(x for x in (p.get("hometown"), p.get("homeprov") or p.get("homecntry")) if x)
    chl = [d for d in (p.get("draftinfo") or []) if d.get("draft_type") != "NHL"]
    cn = (p.get("homecntry") or "").strip()
    imp = any(d.get("draft_type") == "Import" for d in chl) or cn not in ("", "Canada", "United States", "USA")
    if chl:
        d = chl[0]
        kind = d.get("draft_type")
        label = "Import Draft" if kind == "Import" else "U18 Draft" if kind == "U18" else LABEL[lg]
        code = str(d.get("draft_team") or "")
        if not d.get("draft_round") or "FA" in code.split():
            who = team_name(code.replace(" FA", ""), lg, codes) or p["_team"]
            yr = d.get("draft_year")
            od = [LABEL[lg], "Free agent · signed by %s%s" % (who, " (%s)" % yr if yr else "")]
        else:
            od = [label, "%s · Round %s, #%s · %s" % (d.get("draft_year"), d.get("draft_round"),
                                                              d.get("draft_rank"), team_name(code, lg, codes))]
    else:
        od = [LABEL[lg], "Free agent · signed by %s" % p["_team"]]
    nh = nhl_line(p) or eligible.get((p.get("birthdate"), num(p.get("weight")))) or "Undrafted"
    e = {"pos": pos, "dob": p.get("birthdate"), "ht": fmt_ht(p.get("height")), "wt": num(p.get("weight")), "home": home}
    if imp:
        e["imp"] = 1
    e["nhl"] = nh
    e["od"] = od
    return e


# ---------------------------------------------------------------- page parsing
def lineup_players(src):
    out = {}
    for key in sorted({k for d in LG_KEYS.values() for k in d.values()}):
        b = U.find_block(src, key)
        if not b:
            continue
        seen, lst = set(), []
        for m in PLAYER.finditer(src[b[0]:b[1]]):
            n = int(m.group(1))
            if n not in seen:
                seen.add(n)
                lst.append((n, json.loads(m.group(2)), json.loads(m.group(3))))
        out[key] = lst
    return out


def find_player(roster, n, first, last):
    nl, nf = norm(last), norm(first)
    c = [x for x in roster if norm(x.get("last_name")) == nl]
    if len(c) > 1:
        c = [x for x in c if norm(x.get("first_name")) == nf] or [x for x in c if num(x.get("tp_jersey_number"), -1) == n] or c
    if c:
        return c[0]
    for x in roster:
        if norm(x.get("first_name")) + norm(x.get("last_name")) == nf + nl:
            return x
    for x in roster:
        if num(x.get("tp_jersey_number"), -1) == n and norm(x.get("first_name"))[:1] == nf[:1]:
            return x
    return None


def full_due(state, now):
    cands = [datetime.datetime.combine(now.date() + datetime.timedelta(days=d), datetime.time(h), tzinfo=U.ET)
             for d in (0, -1) for h in SLOTS]
    latest = max(c for c in cands if c <= now)
    last = state.get("players_full")
    if not last:
        return True
    return datetime.datetime.fromisoformat(last) < latest


def main():
    args = sys.argv[1:]
    dry, force = "--dry" in args, "--full" in args
    now = datetime.datetime.now(U.ET)
    state = json.load(open(U.STATE)) if os.path.exists(U.STATE) else {}
    src = open(U.INDEX, encoding="utf-8").read()
    i = src.index("var PL = ") + len("var PL = ")
    PL, n = json.JSONDecoder().raw_decode(src[i:])
    end = i + n
    lineup = lineup_players(src)
    missing = {k: [p for p in lst if str(p[0]) not in PL.get(k, {})] for k, lst in lineup.items()}
    missing = {k: v for k, v in missing.items() if v}
    full = force or full_due(state, now)
    if not missing and not full:
        print("pop-ups complete and stats current; nothing to do")
        return
    print("lineup players without a pop-up:", sum(len(v) for v in missing.values()), "| full refresh:", full)

    codes = load_codes()
    eligible = {}
    for k in PL:
        for e in PL[k].values():
            if e.get("nhl") not in (None, "Undrafted"):
                eligible[(e.get("dob"), e.get("wt"))] = e["nhl"]
    added = refreshed = failed = 0
    done_full = full
    for lg, kd in LG_KEYS.items():
        keys = set(kd.values())
        want = None if full else {k for k in missing if k in keys}
        if want is not None and not want:
            continue
        try:
            got = load_league(lg, codes, want)
        except Exception as e:
            print(lg, "feed failed:", e)
            done_full = False
            continue
        if not got:
            done_full = False
            continue
        rosters, stats = got

        def row_for(p):
            return stats["goalies" if p.get("position") == "G" else "skaters"].get(str(p.get("player_id")))

        for key, ros in rosters.items():
            PL.setdefault(key, {})
            players = lineup.get(key, []) if full else missing.get(key, [])
            for (n_, first, last) in players:
                p = find_player(ros, n_, first, last)
                if not p:
                    print("  no roster match:", key, n_, first, last)
                    failed += 1
                    continue
                e = PL[key].get(str(n_))
                if not e or e.get("dob") != p.get("birthdate"):
                    e = build_entry(p, lg, codes, eligible)
                    e["st"] = stat_list(p.get("position") == "G", row_for(p))
                    PL[key][str(n_)] = e
                    added += 1
                    print("  added", key, n_, first, last)
            if full:
                byid = {(str(num(p.get("tp_jersey_number"), -1)), p.get("birthdate")): p for p in ros}
                for n_, e in PL[key].items():
                    p = byid.get((n_, e.get("dob")))
                    if not p:
                        continue
                    st = stat_list(e.get("pos") == "G", row_for(p))
                    if e.get("st") != st:
                        e["st"] = st
                        refreshed += 1
                    nh = nhl_line(p)
                    if nh and e.get("nhl") != nh:
                        e["nhl"] = nh
    print("added %d, stats refreshed for %d, unmatched %d" % (added, refreshed, failed))
    if dry:
        print("dry run: nothing written")
        return
    out = src[:i] + U.js(PL) + src[end:]
    if out != src:
        open(U.INDEX, "w", encoding="utf-8").write(out)
    if done_full:
        state["players_full"] = now.isoformat()
        json.dump(state, open(U.STATE, "w"), indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
