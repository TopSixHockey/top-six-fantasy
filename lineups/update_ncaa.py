#!/usr/bin/env python3
"""Adds an "NCAA" line to player pop-ups for players with an NCAA commitment.

Sources, in order of priority:
  1. lineups/ncaa_commitments.json  - a hand-kept list of {"name", "school", "year"} (optional "team" key, "dob");
                                       it always wins, so use it to add or correct a player
  2. collegecommitments.com         - the public RinkNet commitments list, read once per run

Players are matched by name, and by their current junior team when the list gives one. If the website can't be read,
the pop-ups stay exactly as they are (nothing is removed).

  python lineups/update_ncaa.py          apply
  python lineups/update_ncaa.py --dry    show what would change
"""
import html as H, json, os, re, sys, unicodedata, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_lineups as U
import update_players as P

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "ncaa_commitments.json")
URL = "https://www.collegecommitments.com/"
UA = {"User-Agent": "TopSixHockey/1.0 (+https://topsixhockey.ca; lineup site; reads the public list once a day)"}
ROW = re.compile(r"<tr[^>]*>\s*<td>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*</tr>")

SCHOOL = {
    "Arizona State University Sun Devils": "Arizona State", "Augustana Univeristy Vikings": "Augustana",
    "Bemidji State University Beavers": "Bemidji State", "Boston College Eagles": "Boston College",
    "Boston University Terriers": "Boston University", "Bowling Green State University Falcons": "Bowling Green",
    "Brown University Bears": "Brown", "Clarkson University Golden Knights": "Clarkson",
    "Colgate University Raiders": "Colgate", "College of the Holy Cross Crusaders": "Holy Cross",
    "Colorado College Tigers": "Colorado College", "Cornell University Big Red": "Cornell",
    "Dartmouth College Big Green": "Dartmouth", "Ferris State University Bulldogs": "Ferris State",
    "Harvard University Crimson": "Harvard", "Lake Superior State Univ. Lakers": "Lake Superior State",
    "Lindenwood University Lions": "Lindenwood", "Long Island University Sharks": "Long Island University",
    "Merrimack College Warriors": "Merrimack", "Miami University Redhawks": "Miami (OH)",
    "Michigan State University Spartans": "Michigan State", "Michigan Tech University Huskies": "Michigan Tech",
    "Minnesota State University Mavericks (Mankato": "Minnesota State", "Niagara University Purple Eagles": "Niagara",
    "Northeastern University Huskies": "Northeastern", "Northern Michigan University Wildcats": "Northern Michigan",
    "Penn State University Nittany Lions": "Penn State", "Princeton University Tigers": "Princeton",
    "Providence College Friars": "Providence", "Quinnipiac University Bobcats": "Quinnipiac",
    "Rensselaer (RPI) Engineers": "RPI", "Robert Morris University Colonials": "Robert Morris",
    "Sacred Heart University Pioneers": "Sacred Heart", "St. Cloud State University Huskies": "St. Cloud State",
    "St. Lawrence University Saints": "St. Lawrence", "The Ohio State University Buckeyes": "Ohio State",
    "U.S. Air Force Academy Falcons": "Air Force", "U.S. Military Academy (Army) Black Knights": "Army",
    "UMass Amherst Minutemen": "UMass", "UMass Lowell River Hawks": "UMass Lowell",
    "Union College Dutchmen": "Union", "University of Alaska-Fairbanks Nanooks": "Alaska Fairbanks",
    "University of Connecticut Huskies": "UConn", "University of Denver Pioneers": "Denver",
    "University of Maine Black Bears": "Maine", "University of Michigan Wolverines": "Michigan",
    "University of Minnesota Golden Gophers": "Minnesota", "University of Minnesota-Duluth Bulldogs": "Minnesota Duluth",
    "University of Nebraska-Omaha Mavericks": "Omaha", "University of New Hampshire Wildcats": "New Hampshire",
    "University of North Dakota Fighting Hawks": "North Dakota", "University of Notre Dame Fighting Irish": "Notre Dame",
    "University of St. Thomas Tommies": "St. Thomas", "University of Vermont Catamounts": "Vermont",
    "University of Wisconsin Badgers": "Wisconsin", "Western Michigan University Broncos": "Western Michigan",
    "Yale University Bulldogs": "Yale",
}


def short_school(c):
    c = H.unescape(c).strip()
    if c in SCHOOL:
        return SCHOOL[c]
    c = re.sub(r"^(The |University of )", "", c)
    c = re.sub(r"\s+(University|College)\b.*$", "", c)   # unknown school: drop "University ..." and the nickname
    return c.strip() or None


def tn(s):
    s = unicodedata.normalize("NFD", str(s or "").replace("œ", "oe").replace("Œ", "oe"))
    s = re.sub(r"[^a-z]", "", s.encode("ascii", "ignore").decode().lower())
    return "soo" + s[13:] if s.startswith("saultstemarie") else s


def fetch_site():
    """-> [{"name", "school", "year", "team"(normalised junior team name)}] or None if the site can't be read."""
    try:
        t = urllib.request.urlopen(urllib.request.Request(URL, headers=UA), timeout=40).read().decode("utf-8", "replace")
    except Exception as e:
        print("ncaa: commitments site not readable, using the hand list only:", e)
        return None
    print("ncaa: page is %d characters, %d table rows" % (len(t), t.count("<tr")), "|", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))[:160])
    out = []
    for m in ROW.finditer(t):
        _, nm, _, col, team, yr = [H.unescape(x).strip() for x in m.groups()]
        tm = re.match(r"(.*)\((OHL|WHL|QMJHL)\)\s*$", team)
        if not tm or "," not in nm:
            continue
        last, first = [x.strip() for x in nm.split(",", 1)]
        sch = short_school(col)
        if sch:
            out.append({"name": first + " " + last, "school": sch, "year": yr, "team": tn(tm.group(1))})
    print("ncaa: read %d CHL commitments from the site" % len(out))
    return out if len(out) >= 100 else None   # a tiny result means the page changed; don't trust it


def main():
    dry = "--dry" in sys.argv
    hand = []
    try:
        hand = json.load(open(FILE, encoding="utf-8"))
    except Exception as e:
        print("ncaa: no hand list:", e)
    site = fetch_site()
    src = open(U.INDEX, encoding="utf-8").read()
    i = src.index("var PL = ") + len("var PL = ")
    PL, n = json.JSONDecoder().raw_decode(src[i:])
    end = i + n
    C = {m.group(1): [json.loads('"' + m.group(2) + '"')] for m in re.finditer(r'^ ([a-z_]+):\["([^"]+)"', src, re.M)}   # team names, as in the page
    lineup = P.lineup_players(src)
    set_n = cleared = 0
    for key, players in lineup.items():
        team = tn((C.get(key) or [""])[0])
        for (num, first, last) in players:
            e = PL.get(key, {}).get(str(num))
            if e is None:
                continue
            nm = P.norm(first + " " + last)
            new = None
            for r in hand:   # the hand list wins
                if P.norm(r.get("name")) == nm and (not r.get("dob") or r["dob"] == e.get("dob")) and (not r.get("team") or r["team"] == key):
                    new = r["school"] + (" · " + r["year"] if r.get("year") else "")
                    break
            if new is None and site is not None:
                same = [r for r in site if P.norm(r["name"]) == nm]
                mine = [r for r in same if r["team"] == team]
                pick = (mine or (same if len(same) == 1 else []))[:1]   # same name on another team only counts if unique (traded)
                if pick:
                    new = pick[0]["school"] + " · " + pick[0]["year"]
            if new and e.get("ncaa") != new:
                e["ncaa"] = new
                set_n += 1
                print("  NCAA:", key, num, first, last, "->", new)
            elif not new and "ncaa" in e and site is not None:
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
