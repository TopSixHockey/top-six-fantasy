(function () {
  var GA_ID = 'G-YHZFDVK3NW';
  var KEY = 'tsh_analytics_consent';
  window.dataLayer = window.dataLayer || [];
  function gtag() { dataLayer.push(arguments); }
  window.gtag = gtag;
  // Everything is denied until the visitor accepts. No Google request is made before that.
  gtag('consent', 'default', { analytics_storage: 'denied', ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied' });

  function read() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function write(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  var loaded = false;
  function enable() {
    window['ga-disable-' + GA_ID] = false;
    gtag('consent', 'update', { analytics_storage: 'granted' });
    if (loaded) return;
    loaded = true;
    var s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + GA_ID;
    document.head.appendChild(s);
    gtag('js', new Date());
    gtag('config', GA_ID, { allow_google_signals: false, allow_ad_personalization_signals: false });
  }
  function disable() {
    window['ga-disable-' + GA_ID] = true;
    gtag('consent', 'update', { analytics_storage: 'denied' });
    var parts = location.hostname.split('.');
    var domains = [null, location.hostname, '.' + location.hostname];
    if (parts.length > 2) domains.push('.' + parts.slice(-2).join('.'));
    document.cookie.split(';').forEach(function (c) {
      var n = c.split('=')[0].trim();
      if (/^_ga(_|$)/.test(n) || n === '_gid') {
        domains.forEach(function (d) {
          document.cookie = n + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/' + (d ? '; domain=' + d : '');
        });
      }
    });
  }

  var box, reopen;
  function build() {
    var st = document.createElement('style');
    st.textContent =
      '#tsh-cc{position:fixed;left:12px;right:12px;bottom:12px;z-index:2147483000;max-width:780px;margin:0 auto;background:#14181f;color:#eef1f5;border:1px solid #3a4352;border-radius:10px;padding:14px 16px;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.45)}' +
      '#tsh-cc p{margin:0 0 8px}#tsh-cc .tsh-2{color:#c3cad6}' +
      '#tsh-cc a{color:#8ab4ff}' +
      '#tsh-cc .tsh-b{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}' +
      '#tsh-cc button{flex:1 1 140px;padding:10px 14px;border-radius:8px;border:1px solid #8ab4ff;background:#1d2634;color:#fff;font:600 14px system-ui,sans-serif;cursor:pointer}' +
      '#tsh-cc button:hover{background:#2a3850}' +
      '#tsh-reopen{position:fixed;left:8px;bottom:6px;z-index:2147482000;background:rgba(20,24,31,.8);color:#c3cad6;border:0;border-radius:6px;padding:3px 8px;font:11px system-ui,sans-serif;cursor:pointer;opacity:.75}' +
      '#tsh-reopen:hover{opacity:1}';
    document.head.appendChild(st);

    box = document.createElement('div');
    box.id = 'tsh-cc';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-label', 'Confidentialit\u00e9 / Privacy');
    box.style.display = 'none';
    box.innerHTML = '<div id="tsh-txt"></div><div class="tsh-b"><button type="button" id="tsh-yes"></button><button type="button" id="tsh-no"></button></div>';
    document.body.appendChild(box);

    reopen = document.createElement('button');
    reopen.id = 'tsh-reopen';
    reopen.type = 'button';
    reopen.textContent = 'Confidentialit\u00e9 / Privacy';
    reopen.style.display = 'none';
    document.body.appendChild(reopen);

    document.getElementById('tsh-yes').onclick = function () { write('granted'); enable(); hide(); };
    document.getElementById('tsh-no').onclick = function () { write('denied'); disable(); hide(); };
    reopen.onclick = function () { inQuebec(function (q) { paint(q); box.style.display = 'block'; reopen.style.display = 'none'; }); };
  }

  var FR_P = '<p><strong>Confidentialit&eacute; et t&eacute;moins.</strong> Nous utilisons Google Analytics pour mesurer la fr&eacute;quentation du site (nombre de visiteurs et provenance approximative). Ces t&eacute;moins restent d&eacute;sactiv&eacute;s tant que vous n&rsquo;avez pas donn&eacute; votre accord. <a href="/privacy.html">Politique de confidentialit&eacute;</a></p>';
  var EN_P = '<p><strong>Privacy &amp; cookies.</strong> We use Google Analytics to measure site traffic (number of visitors and approximate location). These cookies stay off unless you accept. <a href="/privacy.html">Privacy policy</a></p>';
  var curFr = /^fr/i.test(navigator.language || '');
  function paint(fr) {
    curFr = fr;
    var a = fr ? FR_P : EN_P, b = fr ? EN_P : FR_P;
    document.getElementById('tsh-txt').innerHTML = a + b.replace('<p>', '<p class="tsh-2">');
    document.getElementById('tsh-yes').textContent = fr ? 'Accepter / Accept' : 'Accept / Accepter';
    document.getElementById('tsh-no').textContent = fr ? 'Refuser / Decline' : 'Decline / Refuser';
    var lbl = fr ? 'Confidentialit\u00e9 / Privacy' : 'Privacy / Confidentialit\u00e9';
    reopen.textContent = lbl;
    box.setAttribute('aria-label', lbl);
    box.setAttribute('lang', fr ? 'fr' : 'en');
  }
  // Is the visitor in Quebec? Approximate IP lookup (no key, no cookies); falls back to browser language.
  function inQuebec(done) {
    var cached = null;
    try { cached = sessionStorage.getItem('tsh_qc'); } catch (e) {}
    if (cached === '1' || cached === '0') return done(cached === '1');
    var fin = false;
    function end(v) {
      if (fin) return; fin = true;
      try { sessionStorage.setItem('tsh_qc', v ? '1' : '0'); } catch (e) {}
      done(v);
    }
    var t = setTimeout(function () { if (!fin) { fin = true; done(curFr); } }, 1800);
    function get(url, test, next) {
      fetch(url, { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
        var q = test(j);
        if (q === null) throw new Error('unknown');
        clearTimeout(t); end(q);
      }).catch(function () { if (next) next(); });
    }
    get('https://get.geojs.io/v1/ip/geo.json', function (j) {
      if (!j || !j.country_code) return null;
      return j.country_code === 'CA' && /qu[e\u00e9]bec/i.test(j.region || '');
    }, function () {
      get('https://ipwho.is/?fields=success,country_code,region,region_code', function (j) {
        if (!j || j.success === false || !j.country_code) return null;
        return j.country_code === 'CA' && (j.region_code === 'QC' || /qu[e\u00e9]bec/i.test(j.region || ''));
      }, null);
    });
  }
  function hide() { box.style.display = 'none'; reopen.style.display = 'block'; }

  function init() {
    build();
    paint(curFr);
    var v = read();
    if (v === 'granted') { enable(); reopen.style.display = 'block'; }
    else if (v === 'denied') { disable(); reopen.style.display = 'block'; }
    else { disable(); inQuebec(function (q) { paint(q); box.style.display = 'block'; }); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
