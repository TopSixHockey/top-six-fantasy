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
      '#tsh-cc p{margin:0 0 8px}#tsh-cc .tsh-en{color:#c3cad6}' +
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
    box.innerHTML =
      '<p><strong>Confidentialit&eacute; et t&eacute;moins.</strong> Nous utilisons Google Analytics pour mesurer la fr&eacute;quentation du site (nombre de visiteurs et provenance approximative). Ces t&eacute;moins restent d&eacute;sactiv&eacute;s tant que vous n&rsquo;avez pas donn&eacute; votre accord. <a href="/privacy.html">Politique de confidentialit&eacute;</a></p>' +
      '<p class="tsh-en"><strong>Privacy &amp; cookies.</strong> We use Google Analytics to measure site traffic (number of visitors and approximate location). These cookies stay off unless you accept. <a href="/privacy.html">Privacy policy</a></p>' +
      '<div class="tsh-b"><button type="button" id="tsh-yes">Accepter / Accept</button><button type="button" id="tsh-no">Refuser / Decline</button></div>';
    document.body.appendChild(box);

    reopen = document.createElement('button');
    reopen.id = 'tsh-reopen';
    reopen.type = 'button';
    reopen.textContent = 'Confidentialit\u00e9 / Privacy';
    reopen.style.display = 'none';
    document.body.appendChild(reopen);

    document.getElementById('tsh-yes').onclick = function () { write('granted'); enable(); hide(); };
    document.getElementById('tsh-no').onclick = function () { write('denied'); disable(); hide(); };
    reopen.onclick = function () { box.style.display = 'block'; reopen.style.display = 'none'; };
  }
  function hide() { box.style.display = 'none'; reopen.style.display = 'block'; }

  function init() {
    build();
    var v = read();
    if (v === 'granted') { enable(); reopen.style.display = 'block'; }
    else if (v === 'denied') { disable(); reopen.style.display = 'block'; }
    else { box.style.display = 'block'; }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
