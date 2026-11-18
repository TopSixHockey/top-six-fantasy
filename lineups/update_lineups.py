#!/usr/bin/env python3
"""Automatic lineup updates for Top Six Hockey (OHL first).

Each run:
  1. Reads the league schedule (HockeyTech feed) and saves tomorrow's puck-drop
     times to lineups/schedule.json so the next day's checks are anticipated.
  2. For every game today whose start time has arrived (or is within 10 min),
     fetches each team's official lineup PDF. When it exists, rewrites that
     team's entry in index.html (L object) and its captains (CAP object).
  3. Flips "Tonight" to "Last game" once a game is final.
Run it often (every 10 min); it does nothing unless a game is due.
"""
import datetime, json, os, re, subprocess, sys, tempfile, urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
SCHED = os.path.join(ROOT, "lineups", "schedule.json")
STATE = os.path.join(ROOT, "lineups", "state.json")
KEY = "f1aa699db3d81487"
FEED = "https://lscluster.hockeytech.com/feed/index.php"
UA = {"User-Agent": "Mozilla/5.0 (TopSixHockey lineup bot)"}
LEAD_MIN = 10      # start checking this many minutes before puck drop
GIVE_UP_H = 6      # stop trying this many hours after puck drop

LEAGUES = {
    "ohl": {"client": "ohl", "season": 88, "label": "OHL", "league_id": 1, "keys": None, "what": "sheet",
            "pdf": "https://cluster.leaguestat.com/game-lineups/ohl/{gid}/{code}_{date}.pdf"},
    "whl": {"client": "whl", "season": 295, "label": "WHL", "league_id": 2, "keys": None, "what": "card",
            "pdf": "https://cluster.leaguestat.com/game-lineups/whl/{gid}/{code}_{date}.pdf"},
}
# team-name keyword -> key used in index.html (L / CAP)
WHL_KEYS = {
 "Brandon": "whl_brandon", "Calgary": "whl_calgary", "Edmonton": "whl_edmonton", "Everett": "whl_everett",
 "Kamloops": "whl_kamloops", "Kelowna": "whl_kelowna", "Lethbridge": "whl_lethbridge",
 "Medicine Hat": "whl_medhat", "Moose Jaw": "whl_moosejaw", "Penticton": "whl_penticton",
 "Portland": "whl_portland", "Prince Albert": "whl_pa", "Prince George": "whl_pg",
 "Red Deer": "whl_reddeer", "Regina": "whl_regina", "Saskatoon": "whl_saskatoon",
 "Seattle": "whl_seattle", "Spokane": "whl_spokane", "Swift Current": "whl_swift",
 "Tri-City": "whl_tricity", "Vancouver": "whl_vancouver", "Victoria": "whl_victoria",
 "Wenatchee": "whl_wenatchee",
}
OHL_KEYS = {
 "Barrie": "barrie", "Brampton": "brampton", "Erie": "erie", "Flint": "flint",
 "Guelph": "guelph", "Hamilton": "hamilton", "Kingston": "kingston",
 "Kitchener": "kitchener", "London": "london", "Niagara": "niagara",
 "North Bay": "northbay", "Oshawa": "oshawa", "Ottawa": "ottawa",
 "Owen Sound": "owensound", "Peterborough": "peterborough", "Saginaw": "saginaw",
 "Sarnia": "sarnia", "Sault": "soo", "Sudbury": "sudbury", "Windsor": "windsor",
 "Brantford": "brampton",
}

def get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        b = r.read()
    return b if binary else b.decode("utf-8", "replace")

def feed(client, view, **kw):
    q = "&".join(f"{k}={v}" for k, v in kw.items())
    t = get(f"{FEED}?feed=modulekit&view={view}&key={KEY}&client_code={client}&lang=en&fmt=json&{q}").strip()
    if t.startswith("("): t = t[1:-1]
    return json.loads(t)["SiteKit"]

def team_key(name, lg="ohl"):
    for k, v in (WHL_KEYS if lg == "whl" else OHL_KEYS).items():
        if k in name: return v
    return None

# ---------------------------------------------------------------- PDF parsing
ROW = re.compile(r"^\s*\d+\s+(\d+)\s+(X\s+)?(G|LD|RD|LW|RW|C|D)\s+(.+?)\s*$")

def parse_card(text):
    """-> dict(players={num:(first,last,pos,starter,mark)}, F=[[lw,c,rw]..], D=[[ld,rd]..])"""
    players, order = {}, []
    lines = text.splitlines()
    split = next((i for i, l in enumerate(lines) if "LINEUP PAIRINGS" in l), len(lines))
    for l in lines[:split]:
        m = ROW.match(l)
        if not m: continue
        num, x, pos, nm = int(m.group(1)), bool(m.group(2)), m.group(3), m.group(4)
        mark = None
        mm = re.search(r"\((C|A)\)", nm)
        if mm: mark = mm.group(1)
        nm = re.sub(r"\s*\([^)]*\)", "", nm).strip()
        nm = re.split(r"\s{2,}", nm)[0].strip()
        last, _, first = nm.partition(",")
        players[num] = (first.strip(), last.strip(), pos, x, mark)
        order.append(num)
    F, D = [], []
    hdr_f = hdr_d = None
    for l in lines[split:]:
        if re.search(r"\bLW\b.*\bC\b.*\bRW\b", l):
            hdr_f = [l.index("LW"), re.search(r"\bC\b", l).start(), l.index("RW")]; mode = "F"; continue
        if re.search(r"\bLD\b.*\bRD\b", l):
            hdr_d = [l.index("LD"), l.index("RD")]; mode = "D"; continue
        m = re.match(r"^(\s*)(\d)\b(.*)$", l)
        if not m: continue
        rest, off = m.group(3), len(m.group(1)) + 1
        nums = [(off + mm.start(), int(mm.group())) for mm in re.finditer(r"\d+", rest)]
        if not nums: continue
        hdr = hdr_f if mode == "F" else hdr_d
        if hdr is None: continue
        row = [None] * len(hdr)
        for pos_, n in nums:
            j = min(range(len(hdr)), key=lambda k: abs(hdr[k] - pos_))
            row[j] = n
        (F if mode == "F" else D).append(row)
    return {"players": players, "order": order, "F": F, "D": D}

def build_entry(card, roster_prev=None):
    P = card["players"]
    def nm(n): return [n, P[n][0], P[n][1]]
    used = set()
    F = []
    for row in card["F"]:
        got = [r for r in row if r is not None and r in P]
        if not got: continue
        used.update(got)
        if len(got) == 3: F.append([nm(n) for n in row])
        elif len(got) == 2: F.append(["h"] + [nm(n) for n in row if n in P] + ["h"])
        else: F.append([nm(n) if n in P and n is not None else None for n in row])
    D = []
    for row in card["D"]:
        got = [r for r in row if r is not None and r in P]
        if not got: continue
        used.update(got)
        D.append([nm(n) for n in row if n is not None and n in P])
    # dressed skaters missing from the pairings table
    left = [n for n in card["order"] if P[n][2] != "G" and n not in used]
    lf = [n for n in left if P[n][2] not in ("LD", "RD", "D")]
    ld = [n for n in left if P[n][2] in ("LD", "RD", "D")]
    for i in range(0, len(lf), 3):
        grp = lf[i:i+3]
        F.append([nm(n) for n in grp] if len(grp) == 3 else (["h"] + [nm(n) for n in grp] + ["h"] if len(grp) == 2 else [None, None, nm(grp[0])]))
    for n in ld: D.append([nm(n)])
    used.update(left)
    gs = [n for n in card["order"] if P[n][2] == "G"]
    gs.sort(key=lambda n: (not P[n][3]))
    G = [nm(n) for n in gs]
    caps = {str(n): P[n][4] for n in P if P[n][4]}
    return {"F": F, "D": D, "G": G}, caps, set(P)

def js(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))

def emit(game, updated, note, ent, S):
    def row(r):
        return "[" + ",".join(js(x) for x in r) + "]"
    f = ",\n   ".join(row(r) for r in ent["F"])
    d = ",".join(row(r) for r in ent["D"])
    return (f'  game: {js(game)}, updated: {js(updated)},\n  note: {js(note)},\n'
            f'  F: [\n   {f}\n  ],\n  D: [{d}],\n  G: {js(ent["G"])},\n  S: {js(S)}\n')

def find_block(src, key):
    m = re.search(r"^ %s: \{\n" % re.escape(key), src, re.M)
    if not m: return None
    # end = first "\n }" at column 1 after start
    e = re.compile(r"^ \},?\n", re.M).search(src, m.end())
    return m.end(), e.start()

def set_game_text(src, key, text=None, prefix=None):
    b = find_block(src, key)
    if not b: return src
    blk = src[b[0]:b[1]]
    if text is not None:
        blk = re.sub(r'game: "[^"]*"', lambda m: "game: " + js(text), blk, 1)
    elif prefix:
        blk = re.sub(r'game: "(Tonight|Last game):', 'game: "%s:' % prefix, blk, 1)
    return src[:b[0]] + blk + src[b[1]:]

def update_cap(src, key, caps):
    i = src.index("var CAP = ") + len("var CAP = ")
    obj, end = json.JSONDecoder().raw_decode(src[i:])
    obj[key] = caps
    return src[:i] + js(obj) + src[i + end:]

# ------------------------------------------------------------------- the run
def game_start(g):
    try:
        return datetime.datetime.fromisoformat(g["GameDateISO8601"])
    except Exception:
        return datetime.datetime.combine(datetime.date.fromisoformat(g["date_played"]),
                                         datetime.time.fromisoformat(g["schedule_time"]), ET)

def fmt_date(d): return d.strftime("%b ") + str(d.day) + d.strftime(", %Y")

def pdf_text(url):
    try:
        b = get(url, binary=True)
    except Exception:
        return None
    if not b.startswith(b"%PDF"): return None
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(b); f.flush()
        return subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout

def main():
    now = datetime.datetime.now(ET)
    if len(sys.argv) > 1: now = datetime.datetime.fromisoformat(sys.argv[1]).replace(tzinfo=ET)
    today, tomorrow = now.date(), now.date() + datetime.timedelta(days=1)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    src = open(INDEX, encoding="utf-8").read()
    sched_out, changed = {}, False
    for lg, cfg in LEAGUES.items():
        try:
            games = feed(cfg["client"], "schedule", season_id=cfg["season"], team_id=0, league_id=cfg["league_id"])["Schedule"]
        except Exception as e:
            print(lg, "schedule fetch failed:", e); continue
        # tomorrow's times (anticipation)
        sched_out[lg] = [{"id": g["game_id"], "time": g["schedule_time"][:5], "et": game_start(g).astimezone(ET).strftime("%H:%M"),
                          "home": g["home_team_name"], "away": g["visiting_team_name"]}
                         for g in games if g["date_played"] == str(tomorrow)]
        sched_out[lg].sort(key=lambda x: x["time"])
        for g in games:
            if g["date_played"] not in (str(today), str(today - datetime.timedelta(days=1))): continue
            start = game_start(g)
            gid = g["game_id"]; final = g.get("final") == "1"
            sides = [("home", g["home_team_name"], g["home_team_code"], g["visiting_team_name"], "vs"),
                     ("away", g["visiting_team_name"], g["visiting_team_code"], g["home_team_name"], "at")]
            for side, tname, code, opp, vsat in sides:
                key = team_key(tname, lg)
                if not key: continue
                sk = f"{lg}:{gid}:{side}"
                st = state.get(sk, {})
                if st.get("done"):
                    if final and not st.get("flipped"):
                        src = set_game_text(src, key, prefix="Last game"); st["flipped"] = True; changed = True
                    state[sk] = st; continue
                due = start - datetime.timedelta(minutes=LEAD_MIN) <= now <= start + datetime.timedelta(hours=GIVE_UP_H)
                if not due: continue
                text = pdf_text(cfg["pdf"].format(gid=gid, code=code, date=g["date_played"]))
                if not text: print("not posted yet:", tname, gid); continue
                card = parse_card(text)
                if len(card["players"]) < 12 or len(card["F"]) < 2:
                    print("could not parse card:", tname, gid); continue
                ent, caps, nums = build_entry(card)
                scr = []
                try:
                    tid = g["home_team"] if side == "home" else g["visiting_team"]
                    ros = feed(cfg["client"], "roster", season_id=cfg["season"], team_id=tid)["Roster"]
                    scr = sorted([[int(r["tp_jersey_number"]), r["first_name"], r["last_name"]] for r in ros
                                  if r.get("active") == "1" and r.get("tp_jersey_number", "").isdigit()
                                  and int(r["tp_jersey_number"]) not in nums])
                except Exception as e:
                    print("roster fetch failed", tname, e)
                d = datetime.date.fromisoformat(g["date_played"])
                label = "Last game" if final else "Tonight"
                game = f"{label}: <b>{vsat} {opp}</b> · {fmt_date(d)}"
                w = cfg["what"]
                note = (f"Lines are from the {cfg['label']}’s official lineup {w} for the " +
                        (f"{d.strftime('%b')} {d.day} game. " if lg == "ohl" else "game. ") +
                        f"The {w} doesn’t list scratches, so the scratch list is the players on the team’s roster "
                        f"who weren’t on the {w} (some may be injured or away).")
                b = find_block(src, key)
                if not b: print("no block for", key); continue
                src = src[:b[0]] + emit(game, fmt_date(d), note, ent, scr) + src[b[1]:]
                src = update_cap(src, key, caps)
                state[sk] = {"done": True, "flipped": final}; changed = True
                print("updated", key, gid)
    json.dump(sched_out, open(SCHED, "w"), indent=1, sort_keys=True)
    json.dump(state, open(STATE, "w"), indent=1, sort_keys=True)
    if changed: open(INDEX, "w", encoding="utf-8").write(src)

if __name__ == "__main__":
    main()
