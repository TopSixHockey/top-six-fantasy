#!/usr/bin/env python3
"""Share pages for every team: /<league>/<team-name>/ plus a lineup picture for link previews.

A share page is a tiny HTML page. Crawlers (X, Facebook, iMessage...) read its preview tags and show the
team's lineup picture; visitors are sent straight on to the normal site with that team selected (/?team=key).
Pictures live in /og/<league>-<team>.jpg and are re-drawn only when that team's lineup changes.

  python lineups/og_cards.py --needs-images   exit 0 if any picture is missing or out of date
  python lineups/og_cards.py --images         draw the missing / outdated pictures (needs playwright)
  python lineups/og_cards.py --pages          write the share pages and sitemap.xml
Safe by design: problems are printed, never fatal to the workflow."""
import hashlib, html, io, json, os, re, subprocess, sys, unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
OGDIR = os.path.join(ROOT, "og")
STATE = os.path.join(OGDIR, "state.json")
SITE = "https://topsixhockey.ca"
ENABLED = {"ohl", "whl", "qmjhl"}                      # leagues that get share pages (more are switched on later)
LG_DIR = {"whl_": "whl", "qmjhl_": "qmjhl"}
STATIC_PAGES = ["/", "/fantasy/", "/privacy.html"]

NODE = r"""
const fs=require('fs'),vm=require('vm');
const s=fs.readFileSync(process.argv[1],'utf8');
function grab(name){
  const i=s.indexOf('var '+name+' = {'); if(i<0) return {};
  let j=s.indexOf('\n};',i); if(j<0) return {};
  const sb={}; vm.runInNewContext(s.slice(i,j+3)+';this.__r='+name+';',sb); return sb.__r||{};
}
const C={}; for(const m of s.matchAll(/^ ([a-z_]+):\["([^"]+)"((?:,"#[0-9a-fA-F]{3,8}")*)/gm)) C[m[1]]=[m[2]].concat((m[3].match(/#[0-9a-fA-F]{3,8}/g)||[]));
console.log(JSON.stringify({L:grab('L'),C}));
"""

def lg_of(key):
    for p, d in LG_DIR.items():
        if key.startswith(p): return d
    return "ohl"

def slug(name):
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", n).strip("-")

def load():
    r = subprocess.run(["node", "-e", NODE, INDEX], capture_output=True, text=True, timeout=60)
    if r.returncode != 0: raise RuntimeError("node failed: " + r.stderr[:300])
    d = json.loads(r.stdout)
    teams = {}
    for key, c in d["C"].items():
        e = d["L"].get(key)
        if not e or not e.get("F") or lg_of(key) not in ENABLED: continue
        lg = lg_of(key)
        sig = hashlib.sha1(json.dumps([e.get("F"), e.get("D"), e.get("G"), e.get("game"), "layout3"], sort_keys=True).encode()).hexdigest()[:10]
        teams[key] = {"key": key, "name": c[0], "colors": c[1:], "lg": lg, "slug": "%s-%s" % (lg, slug(c[0])),
                      "game": re.sub(r"<[^>]+>", "", e.get("game", "")), "sig": sig}
    return teams

def img_path(t): return os.path.join(OGDIR, t["slug"] + ".jpg")

def state():
    try: return json.load(open(STATE))
    except Exception: return {}

def stale(teams):
    st = state()
    return [t for t in teams.values() if st.get(t["key"]) != t["sig"] or not os.path.exists(img_path(t))]

# ----------------------------------------------------------------------------- picture
def _font(size):
    from PIL import ImageFont
    for d in ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/", "/usr/share/fonts/truetype/liberation/",
              "/usr/share/fonts/truetype/liberation2/", "/usr/share/fonts/liberation/"]:
        for n in ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]:
            if os.path.exists(d + n): return ImageFont.truetype(d + n, size)
    return ImageFont.load_default()

def _wrap(draw, text, font, width):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width or not cur: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def compose(t, forwards, defence, goalies=None):
    from PIL import Image, ImageDraw
    W, H, M, STRIP = 1200, 630, 24, 84
    im = Image.new("RGB", (W, H), (11, 18, 32))
    fw = Image.open(io.BytesIO(forwards)).convert("RGB")
    df = Image.open(io.BytesIO(defence)).convert("RGB")
    gl = Image.open(io.BytesIO(goalies)).convert("RGB") if goalies else None
    gap = 24
    avail_h = H - M - STRIP - M
    if gl is None:
        sl = sr = min(avail_h / max(fw.height, df.height), (W - 2 * M - gap) / (fw.width + df.width))   # as big as fits
    else:                                              # left: forwards. right: defence with goalies + scratches under it
        sl = avail_h / fw.height
        sr = min(sl, avail_h / (df.height + 12 + gl.height))
        k = min(1.0, (W - 2 * M - gap) / (fw.width * sl + df.width * sr))
        sl *= k; sr *= k
    fw = fw.resize((int(fw.width * sl), int(fw.height * sl)), Image.LANCZOS)
    df = df.resize((int(df.width * sr), int(df.height * sr)), Image.LANCZOS)
    x0 = (W - (fw.width + gap + df.width)) // 2
    im.paste(fw, (x0, M)); im.paste(df, (x0 + fw.width + gap, M))
    if gl is not None:
        gl = gl.resize((int(gl.width * sr), int(gl.height * sr)), Image.LANCZOS)
        im.paste(gl, (x0 + fw.width + gap, M + df.height + int(12 * sr)))
    d = ImageDraw.Draw(im)
    y = H - STRIP + 4
    col = t["colors"][0] if t["colors"] else "#f2b705"
    if col.lower() in ("#0b1220", "#000000", "#111111", "#111"): col = "#f2b705"
    d.rectangle([x0, y, x0 + 6, H - M], fill=col)
    f1, f2 = _font(32), _font(20)
    d.text((x0 + 20, y - 4), t["name"], font=f1, fill=(255, 255, 255))
    d.text((x0 + 20, y + 36), t["game"][:70], font=f2, fill=(170, 182, 200))
    tw = d.textlength("topsixhockey.ca", font=f2)
    d.text((W - x0 - tw, y + 36), "topsixhockey.ca", font=f2, fill=(120, 134, 156))
    out = io.BytesIO(); im.save(out, "JPEG", quality=84, optimize=True, progressive=True)
    return out.getvalue()

def make_images(teams):
    todo = stale(teams)
    if not todo: print("og: pictures up to date"); return
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from shots import Shooter
    os.makedirs(OGDIR, exist_ok=True)
    st = state(); n = 0
    with Shooter() as sh:
        for t in todo:
            try:
                got = dict(sh.shots(t["key"]))
                if "forwards" not in got or "defence" not in got: print("og: skipped", t["key"], "(panel missing)"); continue
                open(img_path(t), "wb").write(compose(t, got["forwards"], got["defence"], got.get("goalies")))
                st[t["key"]] = t["sig"]; n += 1; print("og: drew", t["slug"])
            except Exception as ex:
                print("og: failed", t["key"], ex)
            json.dump(st, open(STATE, "w"), indent=1, sort_keys=True)
    print("og: drew %d of %d" % (n, len(todo)))

# ----------------------------------------------------------------------------- pages
PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{name} Line Combinations | Top Six Hockey</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{desc}">
<link rel="canonical" href="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Top Six Hockey">
<meta property="og:title" content="{name} lineup | Top Six Hockey">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{img}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:site" content="@TopSixHockey6">
<meta name="twitter:title" content="{name} lineup | Top Six Hockey">
<meta name="twitter:description" content="{desc}">
<meta name="twitter:image" content="{img}">
<script>location.replace("/?team={key}");</script>
</head><body style="margin:0;background:#0b1220;color:#fff;font:16px system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center">
<p>Opening the {name} lineup&hellip; <a style="color:#f2b705" href="/?team={key}">Continue</a></p>
</body></html>
"""

def write_if_changed(path, text):
    try:
        if open(path, encoding="utf-8").read() == text: return False
    except Exception: pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(text); return True

def make_pages(teams):
    changed = 0; urls = []
    for t in sorted(teams.values(), key=lambda t: t["slug"]):
        have = os.path.exists(img_path(t)) and state().get(t["key"]) == t["sig"]
        img = "%s/og/%s.jpg?v=%s" % (SITE, t["slug"], t["sig"]) if have else SITE + "/image.jpg"
        url = "%s/%s/%s/" % (SITE, t["lg"], t["slug"][len(t["lg"]) + 1:])
        desc = "%s lines: forwards, defence pairings, goalies and scratches. %s." % (t["name"], t["game"]) if t["game"] else \
               "%s lines: forwards, defence pairings, goalies and scratches." % t["name"]
        txt = PAGE.format(name=html.escape(t["name"]), desc=html.escape(desc, quote=True), url=url, img=img, key=t["key"])
        if write_if_changed(os.path.join(ROOT, t["lg"], t["slug"][len(t["lg"]) + 1:], "index.html"), txt): changed += 1
        urls.append(url)
    sm = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + \
         "".join("  <url><loc>%s</loc></url>\n" % u for u in [SITE + p for p in STATIC_PAGES] + urls) + "</urlset>\n"
    if write_if_changed(os.path.join(ROOT, "sitemap.xml"), sm): changed += 1
    print("og: pages written/updated:", changed, "of", len(urls) + 1)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--pages"
    try:
        teams = load()
        if mode == "--needs-images": sys.exit(0 if stale(teams) else 1)
        elif mode == "--images": make_images(teams)
        else: make_pages(teams)
    except SystemExit: raise
    except Exception as ex:
        print("og: skipped:", ex)
