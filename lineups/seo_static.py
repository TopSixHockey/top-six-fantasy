#!/usr/bin/env python3
"""Keeps a plain-text copy of every team's current lineup inside index.html.

The visible site draws lineups with JavaScript; search engines and AI crawlers that do not run
scripts see nothing. This writes the same lineups as ordinary HTML text, visually hidden (the usual
"screen-reader only" technique), between two marker comments right before </body>.
Safe by design: any problem leaves index.html untouched and exits 0 so the workflow never fails."""
import html, json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
START, END = "<!--SEO-LINEUPS-START-->", "<!--SEO-LINEUPS-END-->"
LEAGUE = {"whl_": "WHL", "qmjhl_": "QMJHL"}

NODE = r"""
const fs=require('fs'),vm=require('vm');
const s=fs.readFileSync(process.argv[1],'utf8');
function grab(name){
  const i=s.indexOf('var '+name+' = {'); if(i<0) return {};
  let j=s.indexOf('\n};',i); if(j<0) return {};
  const sb={}; vm.runInNewContext(s.slice(i,j+3)+';this.__r='+name+';',sb); return sb.__r||{};
}
// C is split in two blocks in the page; collect every "key:[name,...]" line instead
const C={}; for(const m of s.matchAll(/^ ([a-z_]+):\["([^"]+)"/gm)) C[m[1]]=m[2];
console.log(JSON.stringify({L:grab('L'),C}));
"""

def league(key):
    for p, n in LEAGUE.items():
        if key.startswith(p): return n
    return "OHL"

def nm(p):
    return "%s %s" % (p[1], p[2]) if isinstance(p, list) and len(p) >= 3 else ""

def players(lst):
    out = []
    for p in lst:
        if isinstance(p, list) and len(p) >= 3 and isinstance(p[0], int):
            out.append(html.escape("#%d %s" % (p[0], nm(p))))
    return out

def team_block(key, name, e):
    h = html.escape
    lg = league(key)
    game = re.sub(r"<[^>]+>", "", e.get("game", ""))
    parts = ['<section><h2>%s line combinations (%s)</h2>' % (h(name), lg)]
    if game: parts.append("<p>%s. Lineup updated %s.</p>" % (h(game), h(e.get("updated", ""))))
    fw = [players([p for p in line if isinstance(p, list)]) for line in e.get("F", [])]
    if any(fw):
        parts.append("<h3>%s forwards</h3><ul>" % h(name))
        parts += ["<li>Line %d: %s</li>" % (i + 1, ", ".join(x)) for i, x in enumerate(fw) if x]
        parts.append("</ul>")
    dm = [players(pr) for pr in e.get("D", [])]
    if any(dm):
        parts.append("<h3>%s defence</h3><ul>" % h(name))
        parts += ["<li>Pair %d: %s</li>" % (i + 1, ", ".join(x)) for i, x in enumerate(dm) if x]
        parts.append("</ul>")
    g = players(e.get("G", []))
    if g: parts.append("<h3>%s goalies</h3><p>%s</p>" % (h(name), ", ".join(g)))
    s = players(e.get("S", []))
    if s: parts.append("<h3>%s scratches</h3><p>%s</p>" % (h(name), ", ".join(s)))
    parts.append("</section>")
    return "".join(parts)

def build(data):
    L, C = data["L"], data["C"]
    items = []
    for key, e in L.items():
        name = C.get(key)
        if name and isinstance(e, dict) and e.get("F"):
            items.append((league(key), name, key, e))
    if not items: return ""
    order = {"OHL": 0, "WHL": 1, "QMJHL": 2}
    items.sort(key=lambda t: (order[t[0]], t[1]))
    body = "".join(team_block(k, n, e) for _, n, k, e in items)
    return ('%s\n<div id="seo-lineups" style="position:absolute;width:1px;height:1px;margin:-1px;padding:0;'
            'overflow:hidden;clip:rect(0,0,0,0);clip-path:inset(50%%);white-space:normal;border:0">'
            '<h2>OHL, WHL and QMJHL line combinations</h2>'
            '<p>Current forwards, defence pairings, goalies and scratches for every Ontario Hockey League, '
            'Western Hockey League and Quebec Maritimes Junior Hockey League team, taken from the official lineup '
            'sheets.</p>%s</div>\n%s\n') % (START, body, END)

def main():
    src = open(INDEX, encoding="utf-8").read()
    r = subprocess.run(["node", "-e", NODE, INDEX], capture_output=True, text=True, timeout=60)
    if r.returncode != 0: print("seo: node failed", r.stderr[:300]); return
    blk = build(json.loads(r.stdout))
    if not blk: print("seo: nothing to write"); return
    if START in src and END in src:
        a = src.index(START); b = src.index(END) + len(END)
        new = src[:a] + blk.rstrip("\n") + src[b:]
    else:
        i = src.rindex("</body>")
        new = src[:i] + blk + src[i:]
    if new != src:
        open(INDEX, "w", encoding="utf-8").write(new); print("seo: lineup text refreshed")
    else: print("seo: no change")

if __name__ == "__main__":
    try: main()
    except Exception as ex: print("seo: skipped:", ex)
