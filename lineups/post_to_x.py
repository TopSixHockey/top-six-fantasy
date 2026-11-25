#!/usr/bin/env python3
"""Posts each team's lineup to X (Twitter) as a picture, as soon as update_lineups.py has saved it.

update_lineups.py appends an item to lineups/x_queue.json every time it saves a new lineup for a game
that has not finished. This script (run by the same workflow, right after the lineups are committed):
  1. photographs the lineups page for each queued team (lineups/shots.py: forwards, defence, goalies+scratches;
     falls back to a 1200x675 card drawn with Pillow if headless Chrome is not available),
  2. uploads the pictures and posts them with a short caption (X API v2, OAuth 1.0a user keys),
  3. removes it from the queue and remembers its id in lineups/x_posted.json so it is never posted twice.

Nothing is posted unless the repository variable X_POSTING is "true" and the four X secrets exist
(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET). Otherwise it only logs what it would post.

Other modes:
  python lineups/post_to_x.py --preview OUTDIR [team_key ...]   draw cards to PNG files, post nothing
  python lineups/post_to_x.py --test team_key                    post ONE real test card for that team
  python lineups/post_to_x.py --backfill 2026-11-22              one-time catch-up: post every team with a lineup for that date
"""
import base64, datetime, hashlib, hmac, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request, uuid
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/Toronto")
# Label by the league's own zone name (the tz database abbreviations don't match what locals call their time).
ZONE_LABEL = {"America/Toronto": "ET", "America/New_York": "ET", "America/Winnipeg": "CT", "America/Regina": "CT",
              "America/Edmonton": "MT", "America/Vancouver": "PT", "America/Los_Angeles": "PT",
              "America/Halifax": "AT", "America/St_Johns": "NT"}

def local_when(item):
    """(time, label) for puck drop in the arena's own time zone, e.g. 7:00 PM MT for a game in Alberta.
    Uses the league's local wall-clock time saved with the queued post; older items and unknown zones stay on Eastern."""
    lab = ZONE_LABEL.get(item.get("zone") or "")
    if lab and item.get("local"):
        try: return datetime.datetime.fromisoformat(item["local"]), lab
        except Exception: pass
    return datetime.datetime.fromisoformat(item["start"]).astimezone(ET), "ET"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
QUEUE = os.path.join(ROOT, "lineups", "x_queue.json")
POSTED = os.path.join(ROOT, "lineups", "x_posted.json")
API = "https://api.x.com/2"
MAX_AGE_H = 4          # a queued lineup older than this is dropped instead of posted late
MAX_PER_RUN = 30       # posts per workflow run (the rest wait for the next run, 10 minutes later)
PAUSE_S = 3            # pause between posts
MAX_TRIES = 3
TAG = {"ohl": "OHL", "whl": "WHL", "qmjhl": "QMJHL"}


# ----------------------------------------------------------------------------- small helpers
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

def hex_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3: h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

def lum(c): return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255

def mix(a, b, t): return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))

def load_colors(src):
    """team key -> [name, body, yoke, stripe1, stripe2, number, outline] from the C table in index.html."""
    # the page keeps the OHL colours and the WHL/QMJHL colours in separate blocks, so read the whole file
    out = {}
    for m in re.finditer(r'^ ([a-z_]+):\["([^"]+)"((?:,"#[0-9a-fA-F]{3,8}")+)\]', src, re.M):
        out[m.group(1)] = [m.group(2)] + re.findall(r"#[0-9a-fA-F]{3,8}", m.group(3))
    return out

def load_entry(src, key):
    """The saved lineup of one team in index.html -> {"F":..,"D":..,"G":..,"game":..} (or None)."""
    m = re.search(r"^ %s: \{\n" % re.escape(key), src, re.M)
    if not m: return None
    e = re.compile(r"^ \},?\n", re.M).search(src, m.end())
    blk = src[m.end():e.start()]
    p = re.search(r"\n  F: (\[.*?\n  \]),\n  D: (\[.*?\]),\n  G: (\[.*?\]),\n  S: ", blk, re.S)
    if not p: return None
    g = re.search(r'game: ("(?:[^"\\]|\\.)*")', blk)
    return {"F": json.loads(p.group(1)), "D": json.loads(p.group(2)), "G": json.loads(p.group(3)),
            "game": json.loads(g.group(1)) if g else ""}

def load_caps(src):
    i = src.index("var CAP = ") + len("var CAP = ")
    return json.JSONDecoder().raw_decode(src[i:])[0]


# ----------------------------------------------------------------------------- the picture
def _font(size, cond=False):
    from PIL import ImageFont
    dirs = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/", "/usr/share/fonts/truetype/liberation/",
            "/usr/share/fonts/truetype/liberation2/", "/usr/share/fonts/liberation/"]
    names = (["DejaVuSansCondensed-Bold.ttf", "LiberationSansNarrow-Bold.ttf"] if cond else []) + \
            ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]
    for d in dirs:
        for n in names:
            if os.path.exists(d + n):
                return ImageFont.truetype(d + n, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()

def _fit(draw, text, maxw, size, minsize, cond=False):
    s = size
    while s > minsize:
        f = _font(s, cond)
        if draw.textlength(text, font=f) <= maxw: return f
        s -= 1
    return _font(minsize, cond)

def _short(first, last):
    last = (last or "").strip()
    if last.isupper(): last = last.title()
    ini = (first or "").strip()[:1].upper()
    return (ini + ". " + last) if ini else last

def _spaced(draw, xy, text, font, fill, gap=3):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + gap

def _players(row):
    return [p for p in row if isinstance(p, list)]

def render_card(item, colors, caps=None, path=None):
    """Draws the lineup card -> PNG bytes (and writes it to path if given)."""
    import io
    from PIL import Image, ImageDraw
    W, H = 1200, 675
    key = item["key"]
    col = colors.get(key) or [item["team"], "#1f2b60", "#1f2b60", "#ffffff", "#ffffff", "#ffffff", "#ffffff"]
    body = hex_rgb(col[1])
    accent = (255, 255, 255)
    for idx in (5, 3, 4):
        c = hex_rgb(col[idx]) if idx < len(col) else None
        if c and lum(c) >= 0.45: accent = c; break
    bg = (11, 20, 32)
    cell = mix(bg, (255, 255, 255), 0.09)
    muted = (160, 175, 195)
    caps = caps or {}
    ent = item["entry"]

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # header
    d.rectangle([0, 0, W, 124], fill=body)
    d.rectangle([0, 124, W, 132], fill=accent)
    title_f = _fit(d, item["team"], 840, 60, 34)
    d.text((40, 20), item["team"], font=title_f, fill=(255, 255, 255))
    when, ab = local_when(item)
    tm = when.strftime("%I:%M %p").lstrip("0")
    sub = "%s %s  ·  %s %d  ·  %s %s" % (item["vsat"], item["opp"], when.strftime("%a %b"), when.day, tm, ab)
    d.text((40, 84), sub, font=_fit(d, sub, 900, 28, 18), fill=(235, 240, 247))
    lg = TAG.get(item["lg"], item["lg"].upper())
    pf = _font(34)
    pw = int(d.textlength(lg, font=pf)) + 44
    d.rounded_rectangle([W - 40 - pw, 36, W - 40, 88], radius=14, fill=mix(body, (255, 255, 255), 0.18))
    d.text((W - 40 - pw / 2, 62), lg, font=pf, fill=(255, 255, 255), anchor="mm")

    # one name size for the whole card (the longest name decides), so the cells look even
    names = [_short(p[1], p[2]) for grp in (ent["F"], ent["D"]) for row in grp for p in _players(row)] + \
            [_short(p[1], p[2]) for p in ent["G"] if isinstance(p, list)]
    name_size = 28
    for nm_ in names:
        s_ = _fit(d, nm_, 171, 28, 15, True).size
        name_size = min(name_size, s_)
    name_font = _font(name_size, True)
    name_font_c = _font(min(name_size, 21), True)      # for short (crowded) cells

    def draw_cell(x, y, w, h, p):
        num, first, last = p[0], p[1], p[2]
        d.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=cell)
        d.rounded_rectangle([x, y, x + 6, y + h], radius=3, fill=accent)
        small = h < 62
        d.text((x + 18, y + (19 if small else 26)), "#%s" % num, font=_font(15 if small else 19), fill=accent, anchor="ls")
        nm = _short(first, last)
        d.text((x + 18, y + h - (9 if small else 13)), nm, font=name_font_c if small else name_font, fill=(255, 255, 255), anchor="ls")
        mark = caps.get(str(num))
        if mark in ("C", "A"):
            cx, cy = x + w - 24, y + 23
            d.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=(242, 182, 50) if mark == "C" else (207, 214, 223))
            d.text((cx, cy + 1), mark, font=_font(17), fill=(20, 24, 31), anchor="mm")

    # forwards (left)
    _spaced(d, (40, 150), "FORWARDS", _font(20), muted)
    FX, CW, GAP = 40, 200, 20
    frows = ent["F"]
    top, bottom = 188, 600
    rh = min(88, (bottom - top) / max(1, len(frows)))
    ch = rh - 10
    for r, row in enumerate(frows):
        y = top + r * rh
        if "h" in row:
            ps = _players(row)
            cols = [(1.0 if len(ps) == 1 else 0.5 + i) for i in range(len(ps))]
        else:
            ps = [p for p in row if isinstance(p, list)]
            cols = [i for i, p in enumerate(row) if isinstance(p, list)]
        for c, p in zip(cols, ps):
            draw_cell(FX + c * (CW + GAP), y, CW, ch, p)

    # defence + goalies (right)
    RX, RW, RG = 720, 205, 15
    _spaced(d, (RX, 150), "DEFENCE", _font(20), muted)
    drows = ent["D"]
    grows = [ent["G"][i:i + 2] for i in range(0, len(ent["G"]), 2)]
    n = len(drows) + len(grows)
    rh2 = min(88, (bottom - top - 44) / max(1, n))
    ch2 = rh2 - 10
    y = top
    for row in drows:
        ps = _players(row)
        cols = [0.5] if len(ps) == 1 else list(range(len(ps)))
        for c, p in zip(cols, ps):
            draw_cell(RX + c * (RW + RG), y, RW, ch2, p)
        y += rh2
    y += 12
    _spaced(d, (RX, y), "GOALIES", _font(20), muted)
    y += 38
    for row in grows:
        for c, p in enumerate(_players(row)):
            draw_cell(RX + c * (RW + RG), y, RW, ch2, p)
        y += rh2

    # footer
    d.line([40, H - 56, W - 40, H - 56], fill=mix(bg, (255, 255, 255), 0.16), width=2)
    d.text((40, H - 28), "topsixhockey.ca", font=_font(24), fill=accent, anchor="lm")
    d.text((W - 40, H - 28), "Official lineup · Top Six Hockey", font=_font(20), fill=muted, anchor="rm")

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    if path:
        with open(path, "wb") as f: f.write(buf.getvalue())
    return buf.getvalue()

TAGS_FILE = os.path.join(ROOT, "lineups", "team_hashtags.json")   # team key -> hashtag (without #); blank = none

def team_tag(key):
    try: tag = str(load(TAGS_FILE, {}).get(key, "") or "")
    except Exception: tag = ""
    tag = re.sub(r"[^0-9A-Za-z_À-ſ]", "", tag)
    return "#" + tag if tag else ""

def caption(item):
    tag = TAG.get(item["lg"], item["lg"].upper())
    tags = " ".join(["#" + tag, "#CHL"] + [x for x in [team_tag(item.get("key", ""))] if x])
    if item.get("date_only"):                      # catch-up posts: the date, no puck-drop time
        d = datetime.date.fromisoformat(item["date_only"])
        when_line = "%s %d" % (d.strftime("%b"), d.day)
    else:
        when, ab = local_when(item)
        when_line = "%s %s · %s %d" % (when.strftime("%I:%M %p").lstrip("0"), ab, when.strftime("%b"), when.day)
    return "\U0001F3D2 %s lines %s %s\n%s\n\n%s" % (item["team"], item["vsat"], item["opp"], when_line, tags)


# ----------------------------------------------------------------------------- X API (OAuth 1.0a)
def _enc(s): return urllib.parse.quote(str(s), safe="~")

def oauth1_header(method, url, ck, cs, tok, ts, extra=None, nonce=None, stamp=None):
    p = {"oauth_consumer_key": ck, "oauth_nonce": nonce or uuid.uuid4().hex,
         "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": str(stamp or int(time.time())),
         "oauth_token": tok, "oauth_version": "1.0"}
    allp = dict(p); allp.update(extra or {})
    base = "&".join([method.upper(), _enc(url),
                     _enc("&".join("%s=%s" % (_enc(k), _enc(v)) for k, v in sorted(allp.items())))])
    sig = base64.b64encode(hmac.new(("%s&%s" % (_enc(cs), _enc(ts))).encode(), base.encode(), hashlib.sha1).digest()).decode()
    p["oauth_signature"] = sig
    return "OAuth " + ", ".join('%s="%s"' % (_enc(k), _enc(v)) for k, v in sorted(p.items()))

def api(method, url, body, creds):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method, headers={
        "Authorization": oauth1_header(method, url, *creds), "Content-Type": "application/json",
        "User-Agent": "TopSixHockey-lineup-bot"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"raw": raw}
    except Exception as e:
        return 0, {"error": str(e)}

def _upload(data, mime, creds):
    """-> (media_id, None) or (None, (outcome, detail))"""
    st, j = api("POST", API + "/media/upload",
                {"media": base64.b64encode(data).decode(), "media_category": "tweet_image", "media_type": mime}, creds)
    if st not in (200, 201):
        return None, classify(st, j, "media upload")
    d = j.get("data") or {}
    mid = d.get("id") or d.get("media_id_string") or j.get("media_id_string") or d.get("media_key")
    if not mid:
        return None, ("fatal", "media upload gave no id: %s" % json.dumps(j)[:300])
    return str(mid), None

def open_shooter():
    """Headless Chrome that photographs the real lineups page (lineups/shots.py). None if it cannot start."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import shots
        return shots.Shooter().__enter__()
    except Exception as e:
        print("screenshots unavailable, posting the drawn card instead:", e)
        return None

def close_shooter(shooter):
    if shooter:
        try: shooter.__exit__(None, None, None)
        except Exception: pass

def post_item(item, colors, caps, creds, prefix="", shooter=None):
    """-> (outcome, detail). outcome: ok | dup | retry | limit | fatal.  Pictures: forwards, defence, goalies+scratches."""
    images = []
    if shooter:
        try: images = [(b, "image/jpeg") for _n, b in shooter.shots(item["key"])]
        except Exception as e: print("screenshots failed for", item["team"], "-", e)
    if not images:
        images = [(render_card(item, colors, caps), "image/png")]
    ids = []
    for data, mime in images:
        mid, err = _upload(data, mime, creds)
        if err: return err
        ids.append(mid)
    st, j = api("POST", API + "/tweets", {"text": prefix + caption(item), "media": {"media_ids": ids}}, creds)
    if st in (200, 201):
        return "ok", (j.get("data") or {}).get("id", "")
    return classify(st, j, "create post")

def classify(st, j, what):
    msg = "%s failed (%s): %s" % (what, st, json.dumps(j)[:400])
    if st == 429: return "limit", msg
    if "duplicate" in json.dumps(j).lower(): return "dup", msg
    if st == 0 or st >= 500: return "retry", msg
    return "fatal", msg


# ----------------------------------------------------------------------------- modes
def item_from_index(src, key, colors):
    ent = load_entry(src, key)
    if not ent: return None
    m = re.search(r"<b>(vs|at) (.*?)</b>", ent["game"])
    lg = "whl" if key.startswith("whl_") else ("qmjhl" if key.startswith("qmjhl_") else "ohl")
    now = datetime.datetime.now(ET).replace(hour=19, minute=0, second=0, microsecond=0)
    return {"id": "preview:" + key, "lg": lg, "key": key, "team": colors.get(key, [key])[0],
            "opp": m.group(2) if m else "TBD", "vsat": m.group(1) if m else "vs", "start": now.isoformat(),
            "entry": {"F": ent["F"], "D": ent["D"], "G": ent["G"]}}

def preview(outdir, keys):
    src = open(INDEX, encoding="utf-8").read()
    colors, caps = load_colors(src), load_caps(src)
    os.makedirs(outdir, exist_ok=True)
    for k in (keys or list(colors)):
        it = item_from_index(src, k, colors)
        if not it: print("no lineup for", k); continue
        render_card(it, colors, caps.get(k), os.path.join(outdir, k + ".png"))
        print(k, "->", caption(it).replace("\n", " | "))

def creds_from_env():
    names = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    vals = [os.environ.get(n, "").strip() for n in names]
    return tuple(vals) if all(vals) else None

def test_post(key):
    creds = creds_from_env()
    if not creds: print("The four X secrets are not all set."); return 1
    src = open(INDEX, encoding="utf-8").read()
    colors, caps = load_colors(src), load_caps(src)
    it = item_from_index(src, key, colors)
    if not it: print("no lineup for", key); return 1
    shooter = open_shooter()
    out, info = post_item(it, colors, caps.get(key), creds, prefix="TEST (please ignore) – ", shooter=shooter)
    close_shooter(shooter)
    print(out, info)
    return 0 if out == "ok" else 1

def backfill(date_iso):
    """One-time catch-up: post every team whose saved lineup is for a game on this date, even if it is already over."""
    creds = creds_from_env()
    if os.environ.get("X_POSTING", "").lower() != "true" or not creds:
        print("X posting is OFF (needs the X_POSTING variable = true and the four X secrets)."); return 1
    d = datetime.date.fromisoformat(date_iso)
    want = "%s %d, %d" % (d.strftime("%b"), d.day, d.year)          # e.g. "Nov 22, 2026"
    src = open(INDEX, encoding="utf-8").read()
    colors, caps = load_colors(src), load_caps(src)
    posted = load(POSTED, [])
    shooter = open_shooter()
    n = 0
    try:
        for key in colors:
            ent = load_entry(src, key)
            if not ent or want not in ent["game"]: continue
            pid = "bf:%s:%s" % (date_iso, key)
            if pid in posted: continue
            m = re.search(r"<b>(vs|at) (.*?)</b>", ent["game"])
            opp = re.sub(r"\s*\((OHL|WHL|QMJHL)\)", "", m.group(2) if m else "TBD").strip(" -–")
            lg = "whl" if key.startswith("whl_") else ("qmjhl" if key.startswith("qmjhl_") else "ohl")
            item = {"id": pid, "lg": lg, "key": key, "team": colors.get(key, [key])[0], "opp": opp,
                    "vsat": m.group(1) if m else "vs", "date_only": date_iso}
            out, info = post_item(item, colors, caps.get(key), creds, shooter=shooter)
            print(item["team"], "->", out, info)
            if out in ("ok", "dup"):
                posted.append(pid); n += 1
                save(POSTED, posted[-500:])
            elif out == "limit":
                print("X rate limit reached - run it again in a few minutes to finish the rest"); break
            time.sleep(PAUSE_S)
    finally:
        close_shooter(shooter)
        save(POSTED, posted[-500:])
    print("catch-up posted", n)
    return 0

def run_queue():
    queue = load(QUEUE, [])
    if not queue: return 0
    posted = load(POSTED, [])
    now = datetime.datetime.now(ET)
    todo = []
    for it in queue:
        if it["id"] in posted: continue
        age = now - datetime.datetime.fromisoformat(it["queued"])
        if age > datetime.timedelta(hours=MAX_AGE_H):
            print("too old, dropped:", it["team"]); continue
        todo.append(it)
    creds = creds_from_env()
    if os.environ.get("X_POSTING", "").lower() != "true" or not creds:
        print("X posting is OFF (needs the X_POSTING variable = true and the four X secrets). Would have posted:",
              ", ".join(i["team"] for i in todo) or "nothing")
        save(QUEUE, []); return 0
    src = open(INDEX, encoding="utf-8").read()
    colors, caps = load_colors(src), load_caps(src)
    left, n = [], 0
    shooter = open_shooter()
    for it in todo:
        if n >= MAX_PER_RUN: left.append(it); continue
        out, info = post_item(it, colors, caps.get(it["key"]), creds, shooter=shooter)
        print(it["team"], "->", out, info)
        n += 1
        if out in ("ok", "dup"):
            posted.append(it["id"])
        elif out == "limit":
            left.append(it); left += todo[todo.index(it) + 1:]; break
        else:
            it["tries"] = it.get("tries", 0) + 1
            if out == "fatal" and it["tries"] >= MAX_TRIES: print("giving up on", it["team"])
            elif out == "retry" or it["tries"] < MAX_TRIES: left.append(it)
            if out == "fatal": left += [x for x in todo[todo.index(it) + 1:] if x not in left]; break
        time.sleep(PAUSE_S)
    close_shooter(shooter)
    save(QUEUE, left)
    save(POSTED, posted[-500:])
    return 0

if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--preview"]: preview(a[1], a[2:])
    elif a[:1] == ["--test"]: sys.exit(test_post(a[1]))
    elif a[:1] == ["--backfill"]: sys.exit(backfill(a[1]))
    else: sys.exit(run_queue())
