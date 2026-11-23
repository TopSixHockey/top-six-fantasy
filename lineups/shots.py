"""Screenshots of the real lineups page for the X posts: one picture each for Forwards, Defence and Goalies+Scratches.

Opens index.html (from the repo checkout, served locally) in headless Chrome, shows one team and crops the same
panels a visitor sees on topsixhockey.ca. Needs: pip install playwright (it drives the Chrome already on the runner).
"""
import contextlib, functools, http.server, os, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEW_W, VIEW_H = 760, 820          # css px; the page's backdrop photo is sized to the window
SCALE = 2                          # sharp on phones
HIDE = "#tsh-cc,#tsh-reopen,#pop,#hist{display:none!important}"

# (what, first heading, heading that ends the group; None = end of the lineup)
GROUPS = [("forwards", "Forwards", "Defence"), ("defence", "Defence", "Goalies"), ("goalies", "Goalies", None)]

JS_RECTS = """
(key) => {
  render(key);
  const out = document.getElementById('out');
  const kids = [...out.children];
  const secs = kids.filter(e => e.classList.contains('sec'));
  const at = t => secs.find(e => e.textContent.trim().toLowerCase() === t.toLowerCase());
  const docY = e => e.getBoundingClientRect().top + window.scrollY;
  const outBottom = out.getBoundingClientRect().bottom + window.scrollY;
  const res = {};
  for (const [name, a, b] of %s) {
    const s = at(a); if (!s) { res[name] = null; continue; }
    const e = b ? at(b) : null;
    res[name] = {top: docY(s) - 6, bottom: (e ? docY(e) - 12 : outBottom - 10)};
  }
  const rk = document.querySelector('.rink').getBoundingClientRect();
  res.x = rk.left + 3; res.w = rk.width - 6;
  return res;
}
""" % str([list(g) for g in GROUPS]).replace("None", "null")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass


class Shooter:
    """with Shooter() as s: s.shots("barrie") -> [(name, jpeg_bytes), ...]"""
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        handler = functools.partial(_Quiet, directory=ROOT)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(channel="chrome", args=["--no-sandbox"])
        except Exception:
            self.browser = self.pw.chromium.launch(args=["--no-sandbox"])
        self.page = self.browser.new_page(viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=SCALE)
        self.page.add_init_script("try{localStorage.setItem('tsh_analytics_consent','denied')}catch(e){}")
        self.page.goto("http://127.0.0.1:%d/index.html" % self.httpd.server_address[1], wait_until="load")
        self.page.add_style_tag(content=HIDE)
        self.page.evaluate("document.fonts.ready.then(()=>1)")
        return self

    def __exit__(self, *a):
        with contextlib.suppress(Exception): self.browser.close()
        with contextlib.suppress(Exception): self.pw.stop()
        with contextlib.suppress(Exception): self.httpd.shutdown()

    def shots(self, key):
        p = self.page
        r = p.evaluate(JS_RECTS, key)
        p.evaluate("document.fonts.ready.then(()=>1)")
        p.wait_for_function("(()=>{const b=document.getElementById('bgimg');return !b||b.complete})()")
        p.wait_for_timeout(700)
        out = []
        for name, _a, _b in GROUPS:
            g = r.get(name)
            if not g: continue
            h = max(60, g["bottom"] - g["top"])
            if h > VIEW_H - 24:                     # unusually long panel: grow the window for it
                p.set_viewport_size({"width": VIEW_W, "height": int(h) + 24})
                p.wait_for_timeout(300)
                r = p.evaluate(JS_RECTS, key)
                g = r[name]; h = g["bottom"] - g["top"]
            p.evaluate("(y)=>window.scrollTo(0,y)", max(0, g["top"] - 8))
            p.wait_for_timeout(250)
            top = p.evaluate("(y)=>y-window.scrollY", g["top"])
            clip = {"x": max(0, r["x"]), "y": max(0, top), "width": min(r["w"], VIEW_W), "height": h}
            out.append((name, p.screenshot(type="jpeg", quality=88, clip=clip)))
            if p.viewport_size["height"] != VIEW_H:
                p.set_viewport_size({"width": VIEW_W, "height": VIEW_H}); p.wait_for_timeout(200)
        return out
