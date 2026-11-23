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

JS_RECT = """
([a, b]) => {
  const out = document.getElementById('out');
  const kids = [...out.children];
  const find = t => kids.find(e => e.classList.contains('sec') && e.textContent.trim().toLowerCase() === t.toLowerCase());
  const s = find(a); if (!s) return null;
  const e = b ? find(b) : null;
  const i0 = kids.indexOf(s), i1 = e ? kids.indexOf(e) : kids.length;
  let bottom = 0;                                   // lowest point of anything drawn in this group (names included)
  for (const el of kids.slice(i0, i1)) {
    bottom = Math.max(bottom, el.getBoundingClientRect().bottom);
    for (const d of el.querySelectorAll('*')) bottom = Math.max(bottom, d.getBoundingClientRect().bottom);
  }
  const cap = e ? e.getBoundingClientRect().top - 6 : out.getBoundingClientRect().bottom;
  const rk = document.querySelector('.rink').getBoundingClientRect();
  return {top: s.getBoundingClientRect().top - 6, bottom: Math.min(bottom + 10, cap),
          x: rk.left + 3, w: rk.width - 6, y: window.scrollY};
}
"""


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
        p.set_viewport_size({"width": VIEW_W, "height": VIEW_H})
        p.evaluate("(k)=>render(k)", key)
        p.evaluate("document.fonts.ready.then(()=>1)")
        p.wait_for_function("(()=>{const b=document.getElementById('bgimg');return !b||b.complete})()")
        p.wait_for_timeout(1500)                    # let fonts, jerseys and the layout settle before measuring
        out = []
        for name, a, b in GROUPS:
            r = p.evaluate(JS_RECT, [a, b])
            if not r: continue
            vh = max(VIEW_H, int(r["bottom"] - r["top"]) + 40)   # window tall enough for the whole panel
            p.set_viewport_size({"width": VIEW_W, "height": vh})
            p.wait_for_timeout(400)
            r = p.evaluate(JS_RECT, [a, b])
            p.evaluate("(y)=>window.scrollTo(0,y)", max(0, r["y"] + r["top"] - 8))
            p.wait_for_timeout(500)
            r = p.evaluate(JS_RECT, [a, b])          # measure again now that it is scrolled into place
            y0 = max(0, r["top"])
            clip = {"x": max(0, r["x"]), "y": y0, "width": min(r["w"], VIEW_W), "height": min(r["bottom"] - y0, vh - y0)}
            out.append((name, p.screenshot(type="jpeg", quality=88, clip=clip)))
        return out
