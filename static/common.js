async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
  return data;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function badge(status) {
  return '<span class="badge ' + esc(status) + '">' + esc(status || 'unknown') + '</span>';
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString();
}

function browserInfo() {
  return {
    language: navigator.language,
    screen_color_depth: screen.colorDepth,
    screen_pixel_depth: screen.pixelDepth,
    screen_height: screen.height,
    screen_width: screen.width,
    window_height: window.innerHeight,
    window_width: window.innerWidth,
    timezone_offset_minutes: new Date().getTimezoneOffset(),
    user_agent: navigator.userAgent,
  };
}

async function showModeBanner(el) {
  try {
    const cfg = await api('/api/config');
    if (cfg.mock) {
      el.className = 'banner warn';
      el.innerHTML = '<b>Mock gateway mode.</b> Requests go to the built-in Flexify simulator, not to the real API.';
    } else if (!cfg.configured) {
      el.className = 'banner bad';
      el.textContent = 'API credentials are not configured on the server: payments are disabled.';
    } else {
      el.className = 'banner';
      el.innerHTML = 'Live gateway: H2H <span class="mono">' + esc(cfg.h2h_host) + '</span>, HPP <span class="mono">' +
        esc(cfg.hpp_host) + '</span>. Callback URL: <span class="mono">' + esc(cfg.callback_url) + '</span>';
    }
    el.classList.remove('hidden');
    return cfg;
  } catch (e) {
    return null;
  }
}
