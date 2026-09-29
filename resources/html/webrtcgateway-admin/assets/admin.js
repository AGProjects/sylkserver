const $ = (id) => document.getElementById(id);
let refreshTimer = null, pollTimer = null, evtSource = null, currentRoom = null, audioTimer = null;

async function api(path, opts) {
  const o = Object.assign({ credentials: 'same-origin', headers: {} }, opts || {});
  const r = await fetch(path, o);
  return r;
}

function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function stripSip(u) {
  return (u == null ? '' : String(u)).replace(/^sips?:/i, '');
}

// Pretty-print a JSON string for display; anything that does not parse is
// returned untouched so a non-JSON blob still shows verbatim.
function prettyJson(s) {
  if (s == null) return '';
  const text = String(s);
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch (e) {
    return text;
  }
}

/* ---------- views ---------- */
function showLogin(configured) {
  stopLive();
  $('app').innerHTML = `
    <div class="login-wrap">
      <div class="card login-card">
        <h2>WebRTC Gateway</h2>
        <p>${configured ? 'Sign in' : 'Admin login is not configured on this server.'}</p>
        <form id="loginForm" action="login" method="post" autocomplete="on" ${configured ? '' : 'style="opacity:.5;pointer-events:none"'}>
          <div class="field"><label>Username</label><input id="u" name="username" autocomplete="username" autofocus></div>
          <div class="field"><label>Password</label><input id="p" name="password" type="password" autocomplete="current-password"></div>
          <button class="btn btn-accent full" type="submit">Sign in</button>
          <div class="err" id="loginErr"></div>
        </form>
      </div>
    </div>`;
  if (configured) {
    $('loginForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      $('loginErr').textContent = '';
      const r = await api('login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: $('u').value, password: $('p').value }),
      });
      if (r.ok) {
        // Ask the browser's password manager to save the credentials.
        // A fetch login has no navigation for the heuristic to catch, so
        // we store explicitly via the Credential Management API (works in
        // a secure context: HTTPS, or localhost).
        if (window.PasswordCredential) {
          try {
            await navigator.credentials.store(new PasswordCredential({
              id: $('u').value, password: $('p').value, name: $('u').value,
            }));
          } catch (e) {}
        }
        boot();
      }
      else {
        const j = await r.json().catch(() => ({}));
        $('loginErr').textContent = j.error === 'invalid credentials' ? 'Invalid username or password.' : (j.error || 'Login failed.');
      }
    });
  }
}

const VIEWS = ['accounts', 'endpoints', 'sessions', 'conferences', 'messages', 'media'];
const DEFAULT_VIEW = VIEWS[0];

// The hash may carry query parameters after the view name, as in
// '#messages?account=alice@example.com&id=<message-id>' (the public
// single-message share link). Split it into the view and its params.
function parseHash() {
  const raw = location.hash.replace(/^#/, '');
  const q = raw.indexOf('?');
  const view = q === -1 ? raw : raw.slice(0, q);
  let params;
  try { params = new URLSearchParams(q === -1 ? '' : raw.slice(q + 1)); }
  catch (e) { params = new URLSearchParams(''); }
  return { view: view, params: params };
}

// A share link is '#messages' plus both an account and a message id.
function sharedMessageTarget() {
  const { view, params } = parseHash();
  if (view !== 'messages') return null;
  const account = (params.get('account') || '').trim();
  const id = (params.get('id') || '').trim();
  return account && id ? { account: account, id: id } : null;
}

// Build the public URL for one message — same page, '#messages' with the
// account and message id as hash parameters.
function shareUrlFor(account, id) {
  return location.origin + location.pathname + location.search +
         '#messages?account=' + encodeURIComponent(account) +
         '&id=' + encodeURIComponent(id);
}

let currentView = VIEWS.includes(parseHash().view) ? parseHash().view : DEFAULT_VIEW;

function switchView(v) {
  if (VIEWS.indexOf(v) === -1) v = DEFAULT_VIEW;
  currentView = v;
  try { history.replaceState(null, '', '#' + v); } catch (e) {}
  if (!$('tab-conferences')) return;   // not on the dashboard (login view)
  VIEWS.forEach(name => {
    $('tab-' + name).classList.toggle('active', name === v);
    $('view-' + name).style.display = name === v ? '' : 'none';
  });
  if (v !== 'conferences') closeDrawer();
  if (v === 'conferences') loadRooms();
  else if (v === 'sessions') loadSessions();
  else if (v === 'endpoints') loadEndpoints();
  else if (v === 'accounts') loadAccounts();
  else if (v === 'media') loadMedia();
  // 'messages' is fully on-demand (count / dump buttons), nothing to preload
  loadCharts(v);
}

const CHARTS = {
  endpoints: ['chart-endpoints', [['connections', '#2563eb', 'connections']], 'Connections per day'],
  accounts: ['chart-accounts', [['registrations', '#2563eb', 'registrations'], ['accounts', '#16a34a', 'unique accounts']], 'Registrations / unique accounts per day'],
  sessions: ['chart-sessions', [['sessions_audio', '#2563eb', 'audio'], ['sessions_video', '#7c3aed', 'video']], 'Sessions per day (audio / video)', 'sessions'],
  conferences: ['chart-conferences', [['conferences', '#2563eb', 'conferences']], 'Conferences per day'],
  messages: ['chart-messages', [['messages', '#2563eb', 'messages']], 'Messages per day'],
};
let metricsCache = { ts: 0, data: null };

async function loadCharts(view) {
  const cfg = CHARTS[view];
  if (!cfg || !$(cfg[0])) return;
  if (!metricsCache.data || Date.now() - metricsCache.ts > 60000) {
    let j = null;
    try {
      const r = await api('metrics/daily?days=30');
      if (r.status === 403) { boot(); return; }
      j = await r.json();
    } catch (e) {}
    if (!j) return;
    metricsCache = { ts: Date.now(), data: j };
  }
  const metrics = metricsCache.data.metrics || {};
  const series = cfg[1].map(([metric, color, label]) => ({ label: label || metric, color, points: metrics[metric] || [] }));
  const totalPoints = cfg[3] ? metrics[cfg[3]] : null;
  renderBarChart($(cfg[0]), series, cfg[2], totalPoints);
}

function renderBarChart(el, series, title, totalPoints) {
  const n = (series[0].points || []).length;
  if (!n) { el.style.display = 'none'; return; }
  el.style.display = '';
  const max = Math.max(1, ...series.flatMap(s => s.points.map(p => p.value)));
  const total = (totalPoints || series[0].points).reduce((a, p) => a + p.value, 0);
  const compact = v => v >= 10000 ? Math.round(v / 1000) + 'k' : v >= 1000 ? (v / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(v);
  const groups = [];
  for (let i = 0; i < n; i++) {
    const day = series[0].points[i].day;
    const label = day.slice(0, 4) + '-' + day.slice(4, 6) + '-' + day.slice(6);
    const tip = label + ' — ' + series.map(s => `${s.points[i].value} ${s.label}`).join(', ');
    const bars = series.map(s => {
      const v = s.points[i].value;
      const h = Math.round(v / max * 100);
      return `<div style="flex:1;height:${v ? Math.max(3, h) : 0}%;background:${s.color};border-radius:2px 2px 0 0;min-height:${v ? '2px' : '0'}"></div>`;
    }).join('');
    const values = series.map(s => s.points[i].value);
    const num = values.some(v => v)
      ? values.map((v, si) => `<span style="color:${series.length > 1 ? series[si].color : '#64748b'}">${compact(v)}</span>`).join('<br>')
      : '';
    groups.push(`<div title="${esc(tip)}" style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;min-width:0">
      <div style="font-size:9px;text-align:center;font-family:ui-monospace,Menlo,monospace;line-height:1.25;margin-bottom:2px;white-space:nowrap;overflow:hidden">${num}</div>
      <div style="display:flex;align-items:flex-end;gap:1px;height:90px">${bars}</div>
    </div>`);
  }
  const first = series[0].points[0].day, last = series[0].points[n - 1].day;
  const fmtDay = d => d.slice(4, 6) + '/' + d.slice(6);
  const legend = series.length > 1
    ? '<span style="margin-left:auto">' + series.map(s =>
        `<span style="font-size:11px;color:#64748b;margin-left:12px"><span style="display:inline-block;width:8px;height:8px;background:${s.color};border-radius:2px;margin-right:4px"></span>${esc(s.label)}</span>`).join('') + '</span>'
    : '';
  el.innerHTML = `
    <div style="display:flex;align-items:baseline">
      <div style="font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)">${esc(title)}</div>
      <span style="font-size:11px;color:#94a3b8;margin-left:10px">${total.toLocaleString()} in 30 days</span>
      ${legend}
    </div>
    <div style="display:flex;gap:2px;align-items:flex-end;margin-top:10px">${groups.join('')}</div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;margin-top:4px">
      <span>${fmtDay(first)}</span><span>${fmtDay(last)}</span>
    </div>`;
}

function showDashboard(username) {
  $('app').innerHTML = `
    <header class="topbar">
      <span class="dot"></span>
      <h1>SylkServer WebRTC Gateway Application Admin frontend</h1>
      <span class="spacer"></span>
      <span class="who">${esc(username || '')}</span>
      <button class="btn btn-light" onclick="logout()">Sign out</button>
    </header>
    <nav class="tabs">
      <button id="tab-accounts" onclick="switchView('accounts')">Accounts</button>
      <button id="tab-endpoints" onclick="switchView('endpoints')">End-points</button>
      <button id="tab-sessions" onclick="switchView('sessions')">Sessions</button>
      <button id="tab-conferences" onclick="switchView('conferences')">Conferences</button>
      <button id="tab-messages" onclick="switchView('messages')">Messages</button>
      <button id="tab-media" onclick="switchView('media')">File transfers</button>
    </nav>
    <main>
      <section id="view-conferences">
        <div class="toolbar">
          <h2>Conferences</h2>
          <span class="count" id="roomCount">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadRooms()">Refresh</button>
        </div>
        <div class="card" id="chart-conferences" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="card">
          <table>
            <thead><tr>
              <th>Room URI</th>
              <th class="hide">Janus Room ID</th>
              <th>Sessions</th>
              <th style="width:30px"></th>
            </tr></thead>
            <tbody id="roomsBody">
              <tr><td colspan="4" class="empty">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section id="view-sessions" style="display:none">
        <div class="toolbar">
          <h2>Sessions</h2>
          <span class="count" id="sessCount">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadSessions()">Refresh</button>
        </div>
        <div class="card" id="chart-sessions" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="card">
          <table>
            <thead><tr>
              <th>Session</th>
              <th class="hide" title="the client's signaling WebSocket (ip:port), not media">Signaling</th>
              <th>Media</th>
              <th>State</th>
              <th>Duration</th>
              <th class="hide">User agent</th>
            </tr></thead>
            <tbody id="sessBody">
              <tr><td colspan="6" class="empty">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section id="view-endpoints" style="display:none">
        <div class="toolbar">
          <h2>End-points</h2>
          <span class="count" id="epCount">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadEndpoints()">Refresh</button>
        </div>
        <div class="card" id="chart-endpoints" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="card">
          <table>
            <thead><tr>
              <th>URI</th>
              <th class="hide">User agent</th>
              <th>Connection</th>
            </tr></thead>
            <tbody id="epBody">
              <tr><td colspan="3" class="empty">Loading…</td></tr>
            </tbody>
          </table>
        </div>
      </section>
      <section id="view-accounts" style="display:none">
        <div class="toolbar">
          <h2>Accounts</h2>
          <span class="count" id="stBackend">—</span>
          <span class="spacer"></span>
          <button class="pbtn" id="delReqBtn" onclick="findDeletionRequests()">Marked for deletion</button>
          <button class="btn btn-accent" onclick="loadAccounts()">Refresh</button>
        </div>
        <div class="card" id="chart-accounts" style="margin-bottom:14px;padding:16px 20px"></div>
        <div id="delReqResult"></div>
        <div class="card" style="padding:16px;margin-bottom:14px">
          <form onsubmit="return lookupAccount(event)" style="display:flex;gap:10px;align-items:center">
            <input id="acctInput" placeholder="user@domain" autocomplete="off"
                   style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Show account</button>
          </form>
          <div id="acctResult"></div>
        </div>
        <div class="stats-grid" id="stGrid">
          <div class="card stat"><div class="lbl">Loading…</div></div>
        </div>
        <div class="hint" id="stHint" style="padding:0 4px"></div>
      </section>
      <section id="view-messages" style="display:none">
        <div class="toolbar">
          <h2>Messages</h2>
          <span class="spacer"></span>
        </div>
        <div class="card" id="chart-messages" style="margin-bottom:14px;padding:16px 20px"></div>
        <div class="stats-grid">
          <div class="card stat">
            <div class="lbl">Chat messages in database</div>
            <div class="num" id="msgCount">—</div>
            <div class="sub">
              <button class="pbtn" id="msgBtn" onclick="countMessages()">Count now</button>
              <span style="display:block;margin-top:8px">full table scan — may take a while on a large database; messages expire after one year (table TTL)</span>
            </div>
          </div>
        </div>
        <div class="card" style="padding:16px;margin-bottom:14px">
          <form onsubmit="return loadMessageTypes(event)" style="display:flex;gap:10px;align-items:center">
            <label for="msgAcctInput" style="font-size:13px;color:#64748b;white-space:nowrap">Account</label>
            <input id="msgAcctInput" placeholder="account (user@domain)" autocomplete="off"
                   style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Show messages</button>
          </form>
          <div id="msgContactRow" style="display:none;gap:10px;align-items:center;margin-top:10px">
            <label for="msgContactSelect" style="font-size:13px;color:#64748b;white-space:nowrap">Contact</label>
            <select id="msgContactSelect" onchange="msgSetContact(this.value)"
                    style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;background:#fff">
              <option value="">All contacts</option>
            </select>
          </div>
          <div id="msgDateRow" style="display:none;gap:10px;align-items:flex-start;margin-top:10px"></div>
          <div id="msgTypesResult"></div>
        </div>
        <div class="card" style="padding:16px">
          <form onsubmit="return dumpMessage(event)" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            <input id="dumpAccount" placeholder="account (user@domain)" autocomplete="off"
                   style="flex:1;min-width:200px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <input id="dumpId" placeholder="message id" autocomplete="off"
                   style="flex:1.4;min-width:240px;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Dump message</button>
          </form>
          <div id="dumpResult"></div>
        </div>
      </section>
      <section id="view-media" style="display:none">
        <div class="toolbar">
          <h2>File transfers</h2>
          <span class="count" id="mediaTotal">—</span>
          <span class="spacer"></span>
          <button class="btn btn-accent" onclick="loadMedia()">Refresh</button>
        </div>
        <div class="stats-grid" id="mediaGrid">
          <div class="card stat"><div class="lbl">Loading…</div></div>
        </div>
        <div class="card" style="padding:16px;margin-bottom:14px">
          <form onsubmit="return loadFileTransfers(event)" style="display:flex;gap:10px;align-items:center">
            <label for="ftAcctInput" style="font-size:13px;color:#64748b;white-space:nowrap">Account</label>
            <input id="ftAcctInput" placeholder="account (user@domain)" autocomplete="off"
                   style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px">
            <button class="btn btn-accent" type="submit">Show transfers</button>
          </form>
          <div id="ftContactRow" style="display:none;gap:10px;align-items:center;margin-top:10px">
            <label for="ftContactSelect" style="font-size:13px;color:#64748b;white-space:nowrap">Contact</label>
            <select id="ftContactSelect" onchange="ftSetContact(this.value)"
                    style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;background:#fff">
              <option value="">All contacts</option>
            </select>
          </div>
          <div id="ftResult"></div>
        </div>
        <div class="hint" style="padding:0 4px">Sizes are walked on the server and cached for two minutes.</div>
      </section>
    </main>`;
  switchView(currentView);
  startLive();
}

/* ---------- data ---------- */
async function loadRooms() {
  const r = await api('rooms');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({ rooms: [] }));
  const rooms = (j.rooms || []).slice().sort((a, b) => (a.uri || '').localeCompare(b.uri || ''));
  $('roomCount').textContent = rooms.length + (rooms.length === 1 ? ' room' : ' rooms');
  const body = $('roomsBody');
  if (!rooms.length) {
    body.innerHTML = `<tr><td colspan="4" class="empty">No live conferences right now.</td></tr>`;
    return;
  }
  body.innerHTML = rooms.map(rm => `
    <tr onclick="openRoom('${encodeURIComponent(rm.uri)}','${esc(rm.uri)}')">
      <td class="mono">${esc(rm.uri)}</td>
      <td class="mono hide">${esc(rm.janus_room_id)}</td>
      <td><span class="pill">${esc(rm.sessions)}</span></td>
      <td class="chev">›</td>
    </tr>`).join('');
}

async function loadEndpoints() {
  const r = await api('endpoints');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({ endpoints: [] }));
  const eps = j.endpoints || [];
  $('epCount').textContent = eps.length + (eps.length === 1 ? ' connection' : ' connections');
  const body = $('epBody');
  if (!body) return;
  if (!eps.length) {
    body.innerHTML = `<tr><td colspan="3" class="empty">No connected end-points right now.</td></tr>`;
    return;
  }
  // One row per account; a connection with no account yet gets a
  // placeholder row so it's still visible.
  const rows = [];
  eps.forEach(ep => {
    if (ep.accounts && ep.accounts.length) {
      ep.accounts.forEach(a => rows.push({
        uri: a.uri, name: a.display_name, ua: a.user_agent,
        reg: a.registration_state, addr: ep.address,
      }));
    } else {
      rows.push({ uri: null, name: null, ua: null, reg: null, addr: ep.address });
    }
  });
  body.innerHTML = rows.map(row => {
    const reg = row.reg === 'registered'
      ? '<span class="badge badge-sip">Registered</span>'
      : row.reg === 'failed'
      ? '<span class="badge badge-muted" style="margin-left:0">Failed</span>'
      : row.reg
      ? `<span class="badge" style="background:#f1f5f9;color:#475569">${esc(row.reg)}</span>`
      : '';
    const name = row.name ? `<div style="font-weight:600">${esc(row.name)} ${reg}</div>` : (reg ? `<div>${reg}</div>` : '');
    const uri = row.uri
      ? `${name}<div class="mono" style="font-size:12px;color:#64748b">${esc(stripSip(row.uri))}</div>`
      : '<span style="color:#94a3b8">(no account yet)</span>';
    return `
    <tr style="cursor:default">
      <td>${uri}</td>
      <td class="hide" style="font-size:13px;color:#475569">${esc(row.ua || '—')}</td>
      <td class="mono">${esc(row.addr || '—')}</td>
    </tr>`;
  }).join('');
}

function fmtDuration(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(2, '0'), ss = String(sec).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return n.toFixed(n >= 100 ? 0 : 1) + ' ' + units[i];
}

async function loadSessions() {
  const r = await api('sessions');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({ sessions: [] }));
  const ss = j.sessions || [];
  $('sessCount').textContent = ss.length + (ss.length === 1 ? ' session' : ' sessions');
  const body = $('sessBody');
  if (!body) return;
  if (!ss.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">No active one-to-one sessions right now.</td></tr>`;
    return;
  }
  // legs of the same call (both parties on this gateway) share a Call-ID —
  // use that to show the peer's user agent on each leg
  const legsByCall = {};
  ss.forEach(s => { if (s.call_id) (legsByCall[s.call_id] = legsByCall[s.call_id] || []).push(s); });
  body.innerHTML = ss.map(s => {
    const peer = s.call_id && (legsByCall[s.call_id] || []).length === 2
      ? legsByCall[s.call_id].find(l => l !== s) : null;
    const incoming = s.direction === 'incoming';
    const stateColors = { established: ['#ecfdf5', '#047857'], accepted: ['#ecfdf5', '#047857'],
                          early_media: ['#fef3c7', '#92400e'], ringing: ['#fef3c7', '#92400e'],
                          progress: ['#fef3c7', '#92400e'], connecting: ['#eff6ff', '#1d4ed8'],
                          terminated: ['#fef2f2', '#b91c1c'] };
    const c = stateColors[s.state] || ['#f1f5f9', '#475569'];
    const state = `<span class="badge" style="background:${c[0]};color:${c[1]}">${esc(s.state || '?')}</span>`;
    const mediaBadges = { audio: '<span class="badge badge-sip">Audio</span>',
                          video: '<span class="badge badge-webrtc">Video</span>' };
    const media = (s.media || []).map(m => mediaBadges[m] || `<span class="badge" style="background:#f1f5f9;color:#475569">${esc(m)}</span>`).join(' ') || '—';
    const rtp = Object.keys(s.media_ports || {}).map(m =>
      `<div class="mono" style="font-size:11px;color:#64748b;margin-top:3px" title="far-end RTP endpoint (${esc(m)})">${esc(m)} ${esc(s.media_ports[m])}</div>`).join('');
    const dir = incoming ? '←' : '→';
    const slow = (s.slow_download ? ' <span style="color:#dc2626;font-size:11px">↓slow</span>' : '')
               + (s.slow_upload ? ' <span style="color:#dc2626;font-size:11px">↑slow</span>' : '');
    const name = s.remote_display_name ? `<div style="font-weight:600">${esc(s.remote_display_name)}${slow}</div>` : (slow ? `<div>${slow}</div>` : '');
    return `
    <tr style="cursor:default">
      <td>
        ${name}
        <div class="mono" style="font-size:12px">${esc(stripSip(s.account || ''))} ${dir} ${esc(stripSip(s.remote_uri || ''))}</div>
        ${s.call_id ? `<div class="mono" style="font-size:11px;color:#94a3b8">${esc(s.call_id)}</div>` : ''}
      </td>
      <td class="mono hide">${esc(s.address || '—')}</td>
      <td>${media}${rtp}</td>
      <td>${state}</td>
      <td class="mono">${fmtDuration(s.duration)}</td>
      <td class="hide" style="font-size:13px;color:#475569">${esc(s.user_agent || '—')}
        ${peer ? `<div style="font-size:11px;color:#94a3b8" title="user agent of the other party (its own leg is listed too)">peer: ${esc(peer.user_agent || '?')}</div>` : ''}
      </td>
    </tr>`;
  }).join('');
}

function statTile(label, value, subLines) {
  const sub = (subLines || []).filter(Boolean).join('<br>');
  return `<div class="card stat">
    <div class="lbl">${label}</div>
    <div class="num">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ''}
  </div>`;
}

async function loadAccounts() {
  const grid = $('stGrid');
  if (!grid) return;
  const r = await api('storage');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { grid.innerHTML = `<div class="card stat"><div class="err">Failed to load account statistics.</div></div>`; return; }
  $('stBackend').textContent = j.backend === 'cassandra' ? 'Cassandra backend' : 'file backend';
  const tiles = [];
  const acc = j.accounts || {};
  tiles.push(acc.error
    ? statTile('Accounts', '—', [`<span class="err">${esc(acc.error)}</span>`])
    : statTile('Accounts', acc.total != null ? acc.total : '—', [
        acc.active_30d != null ? `${acc.active_30d} active last 30 days · ${acc.active_7d} last 7 days` : null,
        acc.with_api_token != null ? `${acc.with_api_token} with history API token` : null,
      ]));
  const pt = j.push_tokens || {};
  const platforms = pt.platforms
    ? Object.keys(pt.platforms).sort().map(k => `${esc(k)}: ${pt.platforms[k]}`).join(' · ')
    : '';
  tiles.push(pt.error
    ? statTile('Mobile push tokens', '—', [`<span class="err">${esc(pt.error)}</span>`])
    : statTile('Mobile push tokens', pt.total != null ? pt.total : '—', [
        pt.accounts != null ? `across ${pt.accounts} account${pt.accounts === 1 ? '' : 's'}` : null,
        platforms,
      ]));
  if (j.public_keys) {
    tiles.push(j.public_keys.error
      ? statTile('PGP public keys', '—', [`<span class="err">${esc(j.public_keys.error)}</span>`])
      : statTile('PGP public keys', j.public_keys.total != null ? j.public_keys.total : '—',
                 ['accounts with an uploaded key']));
  }
  grid.innerHTML = tiles.join('');
  const backend = j.backend === 'cassandra' && j.cassandra
    ? `Cassandra keyspace <b>${esc(j.cassandra.keyspace || '?')}</b> @ ${esc((j.cassandra.contact_points || []).join(', '))} · tokens table: ${esc(j.cassandra.push_tokens_table)}`
    : `File storage in <span class="mono">${esc(j.storage_dir || '?')}</span>`;
  $('stHint').innerHTML = `${backend}<br>${esc(j.messages_note || '')}`;
}

function openAccount(encAccount) {
  $('acctInput').value = decodeURIComponent(encAccount);
  lookupAccount();
  const el = document.getElementById('acctResult');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function findDeletionRequests() {
  const btn = $('delReqBtn'), el = $('delReqResult');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
  el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px"><div class="empty" style="padding:10px">Scanning message store for account deletion requests…</div></div>`;
  let j = null;
  try {
    const r = await api('accounts/marked-for-deletion');
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if (btn) { btn.disabled = false; btn.textContent = 'Marked for deletion'; }
  if (!j) { el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px"><div class="err">Scan failed.</div></div>`; return; }
  if (j.error) { el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px"><div class="err">${esc(j.error)}</div></div>`; return; }
  if (!j.total) {
    el.innerHTML = `<div class="card" style="padding:16px;margin-bottom:14px">
      <b style="font-size:14px">No accounts marked for deletion</b>
      <div style="font-size:12px;color:#64748b;margin-top:4px">no <span class="mono">${esc(j.content_type)}</span> messages found · scanned in ${esc(j.elapsed)}s</div>
    </div>`;
    return;
  }
  const rows = j.accounts.map(a => `
    <tr style="cursor:default">
      <td class="mono" style="font-size:13px;padding:8px 16px 8px 0">
        <a href="#" style="color:var(--accent)" onclick="openAccount('${encodeURIComponent(a.account)}');return false">${esc(a.account)}</a>
      </td>
      <td class="mono" style="font-size:13px;text-align:right;padding:8px 16px 8px 0">${a.requests}</td>
      <td class="mono" style="font-size:12px;color:#64748b;white-space:nowrap;padding:8px 0">${esc(a.last_request || '—')}</td>
    </tr>`).join('');
  el.innerHTML = `
    <div class="card" style="padding:16px;margin-bottom:14px">
      <b style="font-size:14px">${j.total} account${j.total === 1 ? '' : 's'} marked for deletion</b>
      <div style="font-size:12px;color:#64748b;margin-top:4px">accounts with <span class="mono">${esc(j.content_type)}</span> messages · scanned in ${esc(j.elapsed)}s · click an account to inspect it</div>
      <table style="width:auto;margin-top:6px"><thead><tr>
        <th style="padding:6px 16px 4px 0">Account</th>
        <th style="padding:6px 16px 4px 0;text-align:right">Requests</th>
        <th style="padding:6px 0 4px">Last request</th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>`;
}

async function lookupAccount(e) {
  if (e) e.preventDefault();
  const account = $('acctInput').value.trim().toLowerCase();
  if (!account) return false;
  $('acctResult').innerHTML = `<div class="empty" style="padding:18px">Looking up ${esc(account)}…</div>`;
  const r = await api('accounts/' + encodeURIComponent(account) + '/info');
  if (r.status === 403) { boot(); return false; }
  const j = await r.json().catch(() => null);
  renderAccount(j);
  return false;
}

function renderAccount(j) {
  const el = $('acctResult');
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return; }
  const m = j.messages || {};
  const tokens = (j.push_tokens || []).map(t => `<tr style="cursor:default">
      <td class="mono" style="font-size:12px;padding:8px 14px 8px 0">${esc(t.app_id || '?')}</td>
      <td class="mono" style="font-size:12px;padding:8px 14px 8px 0">${esc(t.device_id || '?')}</td>
      <td style="font-size:12px;padding:8px 14px 8px 0">${esc(t.platform || '')}</td>
      <td class="mono" style="font-size:12px;padding:8px 14px 8px 0;word-break:break-all">${esc(t.token || '')}</td>
      <td style="padding:8px 0;text-align:right">
        <button class="pbtn pbtn-kick" onclick="deleteToken('${encodeURIComponent(j.account)}','${encodeURIComponent(t.app_id || '')}','${encodeURIComponent(t.device_id || '')}',this)">Delete</button>
      </td>
    </tr>`).join('');
  const keys = (j.public_keys || []).map(k => `<pre style="font-size:11px;line-height:1.45;background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;margin:8px 0 0">${esc(k)}</pre>`).join('');
  el.innerHTML = `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <h3 style="margin:0;font-size:16px" class="mono">${esc(j.account)}</h3>
        ${j.found ? '<span class="badge badge-sip">message storage enabled</span>'
                  : '<span class="badge badge-muted" style="margin-left:0">message storage not enabled</span>'}
        <span class="spacer" style="flex:1"></span>
        <button class="pbtn pbtn-kick" onclick="purgeAccount('${encodeURIComponent(j.account)}')">Purge account</button>
      </div>
      ${j.found ? `
      <div style="margin-top:10px;font-size:13px;color:#334155">
        ${j.api_token ? `<div><b>API token:</b> <span class="mono" style="font-size:12px;word-break:break-all">${esc(j.api_token)}</span>${j.token_ttl != null ? ` <span style="color:#64748b">· TTL ${esc(j.token_ttl)}</span>` : ''}</div>` : ''}
        ${j.last_login ? `<div style="margin-top:4px"><b>Last login:</b> ${esc(j.last_login)}</div>` : ''}
      </div>` : ''}
      <div style="margin-top:14px">
        <b style="font-size:14px">${m.total != null ? Number(m.total).toLocaleString() : 0} messages stored</b>
        ${m.unread_text != null ? `<span style="font-size:13px;color:#475569"> · ${m.unread_text} unread text message${m.unread_text === 1 ? '' : 's'}</span>` : ''}
        ${m.total ? `<div style="font-size:12px;color:#64748b;margin-top:4px">
          <a href="#messages" style="color:var(--accent)" onclick="openMessagesFor('${encodeURIComponent(j.account)}');return false">show breakdown by type in the Messages tab →</a>
        </div>` : ''}
      </div>
      <div style="margin-top:14px">
        <b style="font-size:14px">${(j.push_tokens || []).length} push token(s) stored</b>
        ${tokens ? `<table style="width:auto;margin-top:2px"><thead><tr>
            <th style="padding:6px 14px 4px 0">App</th><th style="padding:6px 14px 4px 0">Device ID</th>
            <th style="padding:6px 14px 4px 0">Platform</th><th style="padding:6px 14px 4px 0">Token</th>
            <th style="padding:6px 0 4px"></th>
          </tr></thead><tbody>${tokens}</tbody></table>` : ''}
      </div>
      <div style="margin-top:14px">
        <b style="font-size:14px">${(j.public_keys || []).length} public key(s) stored</b>
        ${keys ? `<details style="margin-top:4px"><summary style="font-size:12px;color:#64748b;cursor:pointer">show key(s)</summary>${keys}</details>` : ''}
      </div>
    </div>`;
}

function openMessagesFor(encAccount) {
  switchView('messages');
  $('msgAcctInput').value = decodeURIComponent(encAccount);
  loadMessageTypes();
}

let msgFilters = { account: '', contact: '', date: '' };
// After an account is loaded the first fetch runs at the top (all dates)
// only to discover the newest day, then re-fetches drilled into that day —
// so the view opens on the latest day's messages and the breadcrumb zooms
// OUT from there instead of the operator drilling in. Set on account load,
// consumed by the first msgFetch response.
let msgAutoDay = false;
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

async function loadMessageTypes(e) {
  if (e) e.preventDefault();
  const account = $('msgAcctInput').value.trim().toLowerCase();
  if (!account) return false;
  msgFilters = { account: account, contact: '', date: '' };
  msgAutoDay = true;   // first response should drill straight to the newest day
  const sel = $('msgContactSelect');
  if (sel) sel.innerHTML = '<option value="">All contacts</option>';
  if ($('msgContactRow')) $('msgContactRow').style.display = 'none';
  if ($('msgDateRow')) $('msgDateRow').style.display = 'none';
  msgFetch();
  return false;
}

function msgSetContact(contact) {
  msgFilters.contact = contact;
  msgFetch();
}

function msgSetDate(date) {
  msgFilters.date = date;
  msgFetch();
}

function msgDateLabel(date) {
  if (!date) return '';
  const year = date.slice(0, 4), month = date.slice(5, 7), day = date.slice(8, 10);
  if (day) return `${+day} ${MONTH_NAMES[+month - 1]} ${year}`;
  if (month) return `${MONTH_NAMES[+month - 1]} ${year}`;
  return year;
}

// Year -> Month -> Day drill-down with per-level counters, like the
// client-side messages date filter.
function msgRenderDates(j) {
  const row = $('msgDateRow');
  if (!row) return;
  const date = j.date || '';
  const counts = j.date_counts || {};
  const year = date.slice(0, 4), month = date.slice(5, 7), day = date.slice(8, 10);
  const crumbs = [];
  crumbs.push(date ? `<a href="#" style="color:var(--accent)" onclick="msgSetDate('');return false">All dates</a>` : '<b>All dates</b>');
  if (year) crumbs.push(month ? `<a href="#" style="color:var(--accent)" onclick="msgSetDate('${year}');return false">${year}</a>` : `<b>${year}</b>`);
  if (month) crumbs.push(day ? `<a href="#" style="color:var(--accent)" onclick="msgSetDate('${year}-${month}');return false">${MONTH_NAMES[+month - 1]}</a>` : `<b>${MONTH_NAMES[+month - 1]}</b>`);
  if (day) crumbs.push(`<b>${+day}</b>`);
  let keys = Object.keys(counts).sort();
  if (!year) keys.reverse();   // most recent year first; months and days in calendar order
  const chips = keys.map(k => {
    const next = !year ? k : !month ? `${year}-${k}` : `${year}-${month}-${k}`;
    const label = !year ? k : !month ? MONTH_NAMES[+k - 1] : +k;
    return `<button class="pbtn" onclick="msgSetDate('${next}')">${label} <span style="opacity:.7;font-weight:500">(${counts[k].toLocaleString()})</span></button>`;
  }).join('');
  row.style.display = 'flex';
  row.innerHTML = `
    <label style="font-size:13px;color:#64748b;white-space:nowrap;padding-top:6px">Date</label>
    <span style="font-size:13px;white-space:nowrap;padding-top:6px">${crumbs.join(' › ')}</span>
    <div style="display:flex;gap:6px;flex-wrap:wrap;flex:1">${chips || '<span class="empty" style="padding:6px 0">no messages at this level</span>'}</div>`;
}

// Per-day message list: full set for the current view plus the
// client-side content-type filter selected in the dropdown above it.
let dayMsgAll = [];
let dayMsgTypeFilter = '';
// Display sort order for the day list. dayMsgAll arrives ascending by
// (created_at, message_id) from the backend (that order is what the
// update-tick folding relies on, so it stays untouched); 'desc' just
// reverses a copy for display. Default: most recent message on top.
let dayMsgOrder = 'desc';

// The day's messages after the content-type filter, in the chosen order.
// Live-location / meet UPDATE ticks stay in this list — in the table they are
// nested (collapsed) under their origin so the main view isn't polluted, but
// the flat list is what the count and the archive download use.
function msgTypeFiltered() {
  return dayMsgTypeFilter
    ? dayMsgAll.filter(m => (m.content_type || 'unknown') === dayMsgTypeFilter)
    : dayMsgAll;
}
function msgDayVisible() {
  const msgs = msgTypeFiltered();
  return dayMsgOrder === 'desc' ? msgs.slice().reverse() : msgs.slice();
}

// One day-list table row. opts.groupId set => this origin has update ticks
// nested under it (render an expander caret in the time cell). opts.child set
// => this is a nested update row (hidden until expanded, tagged + indented).
function msgDayRow(m, opts) {
  opts = opts || {};
  const account = msgFilters.account;
  const dirArrow = d => d === 'outgoing' ? '→' : d === 'incoming' ? '←' : '';
  const isChild = !!opts.child;
  const trStyle = 'cursor:default' + (m.is_update ? ';opacity:.72' : '') + (isChild ? ';display:none' : '');
  const trAttr = isChild ? ` data-upd="${opts.child}"` : '';
  const time = esc((m.created_at || '').slice(11, 19) || m.created_at || '');
  const timeCell = opts.groupId
    ? `<button class="pbtn" type="button" data-open="0" data-n="${opts.count}" onclick="msgToggleUpdates(${opts.groupId},this)" title="show/hide ${opts.count} update tick(s)" style="padding:0 6px;font-size:11px;margin-right:6px;min-width:34px">▸ ${opts.count}</button>${time}`
    : (isChild ? '<span style="color:#cbd5e1;margin-right:6px">↳</span>' : '') + time;
  const timePad = isChild ? '7px 16px 7px 20px' : '7px 16px 7px 0';
  return `
        <tr style="${trStyle}"${trAttr}>
          <td class="mono" style="font-size:12px;white-space:nowrap;padding:${timePad}" title="${esc(m.created_at)}${m.timestamp ? ' · msg ' + esc(m.timestamp) : ''}">${timeCell}</td>
          <td style="padding:7px 16px 7px 0;font-size:13px" title="${esc(m.direction)}">${dirArrow(m.direction)}</td>
          <td class="mono" style="font-size:12px;padding:7px 16px 7px 0${(m.contact || '').includes('@') ? '' : ';color:#94a3b8'}">${esc(m.contact)}</td>
          <td class="mono" style="font-size:12px;padding:7px 16px 7px 0">${esc(m.content_type)}</td>
          <td class="mono" style="font-size:12px;padding:7px 16px 7px 0">${(m.action || m.related_action) ? esc(m.action || m.related_action) + (m.action && m.encrypted ? ' <span style="color:#94a3b8" title="encrypted — action inferred">(enc)</span>' : '') : '<span style="color:#cbd5e1">—</span>'}</td>
          <td style="font-size:12px;color:#64748b;padding:7px 16px 7px 0">${esc(m.state)}</td>
          <td style="padding:7px 0"><a href="#" class="mono" style="font-size:12px;color:var(--accent);word-break:break-all" onclick="msgDump('${encodeURIComponent(account)}','${encodeURIComponent(m.message_id)}');return false">${esc(m.message_id)}</a></td>
        </tr>`;
}

// The share-start actions an update trail nests under.
const MSG_ORIGIN_ACTIONS = new Set(['location_start', 'meeting_start', 'meeting_request']);

// Build the day-list body: standalone events at top level, each share's
// update ticks nested (collapsed) under its origin. The origin is picked from
// the session's own data (the start action, else its earliest tick), so the
// nesting is independent of the display sort order — the stop/end row and the
// second meet leg stay as their own top-level rows. Update ticks whose origin
// isn't in the current view fall back to top level so nothing is hidden.
function msgDayRowsHtml() {
  const msgs = msgTypeFiltered();
  if (!msgs.length)
    return `<tr><td colspan="7" class="empty">${dayMsgTypeFilter ? 'No messages of this content type on this day.' : 'No messages on this day.'}</td></tr>`;
  const updBySession = new Map();   // session -> [update rows]
  const nonUpdBySession = new Map();  // session -> [origin/signal rows]
  for (const m of msgs) {
    if (!m.session) continue;
    const bag = m.is_update ? updBySession : nonUpdBySession;
    if (!bag.has(m.session)) bag.set(m.session, []);
    bag.get(m.session).push(m);
  }
  // Anchor (message id) each update trail nests under: the start action if
  // present, otherwise the session's earliest non-update row.
  const anchorBySession = new Map();
  for (const [session, rows] of nonUpdBySession) {
    if (!updBySession.has(session)) continue;   // no updates -> nothing to nest
    let anchor = rows.find(r => MSG_ORIGIN_ACTIONS.has(r.related_action));
    if (!anchor) anchor = rows.slice().sort((a, b) =>
      (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0))[0];
    anchorBySession.set(session, anchor.message_id);
  }
  // top-level rows: everything except update ticks that have an anchor here
  let mains = msgs.filter(m => !m.is_update || !anchorBySession.has(m.session));
  mains = dayMsgOrder === 'desc' ? mains.slice().reverse() : mains.slice();
  const order = list => dayMsgOrder === 'desc' ? list.slice().reverse() : list;
  let html = '', gid = 0;
  for (const m of mains) {
    const isAnchor = !m.is_update && m.session
      && anchorBySession.get(m.session) === m.message_id;
    const kids = isAnchor ? (updBySession.get(m.session) || []) : [];
    if (kids.length) {
      const id = ++gid;
      html += msgDayRow(m, { groupId: id, count: kids.length });
      for (const k of order(kids)) html += msgDayRow(k, { child: id });
    } else {
      html += msgDayRow(m, {});
    }
  }
  return html;
}

// Expand / collapse one origin's nested update ticks.
function msgToggleUpdates(gid, btn) {
  const rows = document.querySelectorAll('tr[data-upd="' + gid + '"]');
  const open = btn.getAttribute('data-open') === '1';
  rows.forEach(r => { r.style.display = open ? 'none' : ''; });
  btn.setAttribute('data-open', open ? '0' : '1');
  btn.textContent = (open ? '▸ ' : '▾ ') + (btn.getAttribute('data-n') || rows.length);
}

// Label for the sort-order toggle button.
function msgDayOrderLabel() {
  return dayMsgOrder === 'desc' ? '↓ Newest first' : '↑ Oldest first';
}

// Re-render just the day-message rows (content-type filter + sort order) —
// the day's messages are already loaded, so no refetch is needed.
function msgRenderDay() {
  const tb = $('msgDayTbody');
  if (!tb) return;
  tb.innerHTML = msgDayRowsHtml();
  const cnt = $('msgDayCount');
  if (cnt) cnt.textContent = Number(msgTypeFiltered().length).toLocaleString();
}

// Content-type dropdown handler.
function msgFilterDay(type) {
  dayMsgTypeFilter = type || '';
  msgRenderDay();
}

// Flip the day-list sort order and re-render in place (no refetch).
function msgToggleDayOrder() {
  dayMsgOrder = dayMsgOrder === 'desc' ? 'asc' : 'desc';
  const btn = $('msgDayOrderBtn');
  if (btn) btn.textContent = msgDayOrderLabel();
  msgRenderDay();
}

// Download the currently visible day messages (after the content-type
// filter) as a JSON archive. Metadata only — day_messages never carries
// the message content, which is end-to-end encrypted anyway.
function msgDownloadDay() {
  const msgs = msgDayVisible();
  if (!msgs.length) { toast('No messages to download'); return; }
  const archive = {
    account: msgFilters.account,
    contact: msgFilters.contact || null,
    date: msgFilters.date || null,
    content_type: dayMsgTypeFilter || null,
    order: dayMsgOrder,
    count: msgs.length,
    note: 'includes raw message content; location coordinates within the payload remain PGP-encrypted',
    messages: msgs.map(m => ({
      message_id: m.message_id,
      created_at: m.created_at,
      timestamp: m.timestamp || null,
      direction: m.direction,
      contact: m.contact,
      content_type: m.content_type,
      action: m.action || null,
      related_action: m.related_action || null,
      metadata: (m.metadata === undefined ? null : m.metadata),
      session: m.session || null,
      is_update: !!m.is_update,
      encrypted: !!m.encrypted,
      state: m.state,
      disposition: m.disposition || [],
      content: (m.content === undefined ? null : m.content),
    })),
  };
  const safe = s => (s || '').replace(/[^a-zA-Z0-9._-]+/g, '_').replace(/^_+|_+$/g, '');
  const parts = ['messages', safe(msgFilters.account), safe(msgFilters.date)];
  if (dayMsgTypeFilter) parts.push(safe(dayMsgTypeFilter));
  const name = parts.filter(Boolean).join('_') + '.json';
  const blob = new Blob([JSON.stringify(archive, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`Downloaded ${msgs.length} message(s)`);
}

async function msgFetch() {
  const account = msgFilters.account;
  const el = $('msgTypesResult');
  if (!el || !account) return;
  el.innerHTML = `<div class="empty" style="padding:18px">Loading message types for ${esc(account)}${msgFilters.contact ? ' with contact ' + esc(msgFilters.contact) : ''}…</div>`;
  const params = [];
  if (msgFilters.contact) params.push('contact=' + encodeURIComponent(msgFilters.contact));
  if (msgFilters.date) params.push('date=' + encodeURIComponent(msgFilters.date));
  const r = await api('messages/types/' + encodeURIComponent(account) + (params.length ? '?' + params.join('&') : ''));
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return; }
  // Reverse drill-down: on the first response after an account load, jump
  // straight to the newest day that has messages (j.newest is the newest
  // stored timestamp) and re-fetch that day. Runs at most once per load, so
  // zooming back OUT via the breadcrumb is never snapped back to the day.
  if (msgAutoDay) {
    msgAutoDay = false;
    if (!msgFilters.date && j.newest) {
      msgFilters.date = String(j.newest).slice(0, 10);
      msgFetch();
      return;
    }
  }
  const contacts = j.contacts || [];
  const contactRow = $('msgContactRow'), dateRow = $('msgDateRow');
  if (!contacts.length) {
    if (contactRow) contactRow.style.display = 'none';
    if (dateRow) dateRow.style.display = 'none';
    el.innerHTML = `<div class="empty" style="padding:18px">No messages stored for ${esc(account)}.</div>`;
    return;
  }
  // account found — show the filters and fill the contact options
  const sel = $('msgContactSelect');
  if (sel) {
    sel.innerHTML = ['<option value="">All contacts</option>']
      .concat(contacts.map(c => `<option value="${esc(c.name)}"${c.name === j.contact ? ' selected' : ''}>${esc(c.name)} (${c.count})</option>`))
      .join('');
  }
  if (contactRow) contactRow.style.display = 'flex';
  msgRenderDates(j);
  const byType = j.by_type || {};
  const types = Object.keys(byType);
  const filtered = j.contact || j.date;
  const scope = `${j.contact ? ` with <span class="mono">${esc(j.contact)}</span>` : ''}` +
                `${j.date ? ` in <span class="mono">${esc(msgDateLabel(j.date))}</span>` : ''}`;
  // Delete rules per content type: text/* only with a full day selected
  // (too easy to wipe whole conversations otherwise), file transfers
  // never (the File transfers tab handles those, files included),
  // anything else always.
  const daySelected = (j.date || '').length === 10;
  const deleteCell = t => {
    if (!j.can_delete) return '';
    if (t.startsWith('application/sylk-file-transfer'))
      return '<span style="font-size:11px;color:#94a3b8">use the File transfers tab</span>';
    if (t.startsWith('text/') && !daySelected)
      return '<span style="font-size:11px;color:#94a3b8">select a day to delete</span>';
    return `<button class="pbtn pbtn-kick" onclick="deleteMessageType('${encodeURIComponent(account)}','${encodeURIComponent(t)}',${byType[t]},this)">Delete</button>`;
  };
  const rows = types.length ? types.map(t => `
    <tr style="cursor:default">
      <td class="mono" style="font-size:13px;text-align:right;width:90px;padding:9px 16px 9px 0">${byType[t].toLocaleString()}</td>
      <td class="mono" style="font-size:13px;padding:9px 16px 9px 0">${esc(t)}</td>
      <td style="text-align:right;padding:9px 0">${deleteCell(t)}</td>
    </tr>`).join('')
    : `<tr><td colspan="3" class="empty">No messages match the current filters.</td></tr>`;
  // When a full day is selected the backend also returns that day's
  // messages (day_messages), sorted by timestamp, without the content —
  // shown as a second table below the type summary.
  const dayMsgs = j.day_messages;
  // Snapshot the day's messages and reset the content-type filter and sort
  // order every time the view reloads (new account / contact / date).
  dayMsgAll = dayMsgs || [];
  dayMsgTypeFilter = '';
  dayMsgOrder = 'desc';   // default: most recent message on top
  const dayList = dayMsgs ? (() => {
    // Content types present in this day's messages, for the filter dropdown.
    const dayTypeCounts = {};
    dayMsgAll.forEach(m => { const t = m.content_type || 'unknown'; dayTypeCounts[t] = (dayTypeCounts[t] || 0) + 1; });
    const typeOpts = [`<option value="">All content types (${dayMsgAll.length})</option>`]
      .concat(Object.keys(dayTypeCounts).sort().map(t =>
        `<option value="${esc(t)}">${esc(t)} (${dayTypeCounts[t]})</option>`)).join('');
    return `
    <div style="margin-top:18px;border-top:1px solid var(--border);padding-top:14px">
      <b style="font-size:14px"><span id="msgDayCount">${Number(dayMsgAll.length).toLocaleString()}</span> message(s) on <span class="mono">${esc(msgDateLabel(j.date))}</span>${j.contact ? ` with <span class="mono">${esc(j.contact)}</span>` : ''}, by time</b>
      <div style="display:flex;gap:10px;align-items:center;margin:10px 0 4px">
        <label for="msgDayTypeSelect" style="font-size:13px;color:#64748b;white-space:nowrap">Content type</label>
        <select id="msgDayTypeSelect" onchange="msgFilterDay(this.value)"
                style="flex:1;padding:9px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;background:#fff">${typeOpts}</select>
        <button class="pbtn" type="button" id="msgDayOrderBtn" onclick="msgToggleDayOrder()" title="toggle sort order" style="white-space:nowrap">${msgDayOrderLabel()}</button>
        <button class="pbtn" type="button" onclick="msgDownloadDay()" style="white-space:nowrap">Download archive</button>
      </div>
      <table style="margin-top:4px"><thead><tr>
        <th style="padding:6px 16px 4px 0">Time</th>
        <th style="padding:6px 16px 4px 0">Dir</th>
        <th style="padding:6px 16px 4px 0">Contact</th>
        <th style="padding:6px 16px 4px 0">Content type</th>
        <th style="padding:6px 16px 4px 0">Action</th>
        <th style="padding:6px 16px 4px 0">State</th>
        <th style="padding:6px 0 4px">Message id</th>
      </tr></thead><tbody id="msgDayTbody">${msgDayRowsHtml()}</tbody></table>
      <div class="hint" style="padding:8px 0 0">live-location and meet update ticks are collapsed under their origin — click the ▸ count in the Time column to expand a share's trail; click a message id to load its full content and metadata in the dump box below</div>
    </div>`;
  })() : '';
  el.innerHTML = `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <b style="font-size:14px">${Number(j.total).toLocaleString()} message(s) for <span class="mono">${esc(account)}</span>${scope}</b>
      ${j.oldest ? `<div style="font-size:12px;color:#64748b;margin-top:4px">oldest <span class="mono">${esc(j.oldest)}</span> · newest <span class="mono">${esc(j.newest)}</span></div>` : ''}
      <table style="margin-top:8px"><thead><tr>
        <th style="text-align:right;padding:6px 16px 4px 0">Count</th>
        <th style="padding:6px 16px 4px 0">Content type</th>
        <th style="padding:6px 0 4px"></th>
      </tr></thead><tbody>${rows}</tbody></table>
      ${j.can_delete ? (filtered ? '<div class="hint" style="padding:8px 0 0">delete removes only the messages matching the current filters</div>' : '') : '<div class="hint" style="padding:8px 0 0">deletion is only available on the Cassandra backend</div>'}
    </div>${dayList}`;
}

// Load a message's full content into the dump box below (used by the
// per-day message list — the id links call this).
function msgDump(encAccount, encId) {
  const acc = decodeURIComponent(encAccount), id = decodeURIComponent(encId);
  if ($('dumpAccount')) $('dumpAccount').value = acc;
  if ($('dumpId')) $('dumpId').value = id;
  dumpMessage();
  const el = $('dumpResult');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function deleteMessageType(encAccount, encType, count, btn) {
  const account = decodeURIComponent(encAccount);
  const type = decodeURIComponent(encType);
  const scope = (msgFilters.contact ? `\nContact: ${msgFilters.contact}` : '') +
                (msgFilters.date ? `\nDate: ${msgDateLabel(msgFilters.date)}` : '');
  if (!confirm(`Permanently delete ${count} ${type} message(s) for ${account}?${scope}\n\nThis cannot be undone.`)) return;
  btn.disabled = true;
  btn.textContent = 'Deleting…';
  let j = null;
  try {
    const r = await api('messages/delete/' + encodeURIComponent(account), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content_type: type, contact: msgFilters.contact,
                             date: msgFilters.date }),
    });
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if (j && j.ok) toast(`Deleted ${j.deleted} ${type} message(s)`);
  else toast('Delete failed' + (j && j.error ? ': ' + j.error : ''));
  msgFetch();   // refresh the breakdown
}

async function purgeAccount(encAccount) {
  const account = decodeURIComponent(encAccount);
  const typed = prompt(
    `This will PERMANENTLY delete all server-side data of ${account}:\n\n` +
    `• all stored messages\n` +
    `• the account entry and its API token\n` +
    `• all push tokens\n` +
    `• the PGP public key\n` +
    `• all files shared by the account\n\n` +
    `This cannot be undone. Type the account address to confirm:`);
  if (typed === null) return;
  if (typed.trim().toLowerCase() !== account) {
    toast('Account address did not match — nothing was deleted');
    return;
  }
  toast('Purging ' + account + '…');
  let j = null;
  try {
    const r = await api('accounts/' + encAccount + '/purge', { method: 'POST' });
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if (j && j.ok) {
    const parts = [];
    if (j.messages != null && j.messages >= 0) parts.push(`${j.messages} messages`);
    if (j.push_tokens != null && j.push_tokens >= 0) parts.push(`${j.push_tokens} push tokens`);
    if (j.public_keys != null && j.public_keys >= 0) parts.push(`${j.public_keys} public keys`);
    parts.push(`${j.transfer_files || 0} shared files (${fmtBytes(j.transfer_bytes || 0)})`);
    toast(`Purged ${account}: ` + parts.join(', '));
  } else {
    toast('Purge failed' + (j && j.error ? ': ' + j.error : ''));
  }
  setTimeout(() => { $('acctInput').value = account; lookupAccount(); }, 1000);
}

async function deleteToken(encAccount, encApp, encDevice, btn) {
  const account = decodeURIComponent(encAccount);
  const app = decodeURIComponent(encApp);
  const device = decodeURIComponent(encDevice);
  if (!confirm(`Purge the push token for app ${app} on device ${device}?\n\n${account} will no longer receive push notifications on that device.`)) return;
  btn.disabled = true;
  btn.textContent = 'Deleting…';
  let ok = false;
  try {
    const r = await api('tokens/' + encAccount + '/' + encApp + '/' + encDevice, { method: 'DELETE' });
    if (r.status === 403) { boot(); return; }
    ok = r.ok;
  } catch (e) {}
  toast(ok ? 'Push token purged' : 'Delete failed');
  // the purge runs async in the storage thread — give it a moment,
  // then refresh the account view
  setTimeout(() => { $('acctInput').value = account; lookupAccount(); }, 1000);
}

async function dumpMessage(e) {
  if (e) e.preventDefault();
  const account = $('dumpAccount').value.trim().toLowerCase();
  const id = $('dumpId').value.trim();
  if (!account || !id) { toast('Enter both account and message id'); return false; }
  $('dumpResult').innerHTML = `<div class="empty" style="padding:18px">Looking up message ${esc(id)}…</div>`;
  const r = await api('messages/dump/' + encodeURIComponent(account) + '/' + encodeURIComponent(id));
  if (r.status === 403) { boot(); return false; }
  const j = await r.json().catch(() => null);
  const el = $('dumpResult');
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return false; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return false; }
  if (!j.found) {
    el.innerHTML = `<div class="empty" style="padding:18px">No message with id <span class="mono">${esc(j.message_id)}</span> for ${esc(j.account)}.</div>`;
    return false;
  }
  el.innerHTML = shareLinkBoxHtml(j.account, j.message_id) +
    (j.messages || []).map(msg => messageCardHtml(msg, j)).join('') +
    (j.scanned ? '<div class="hint" style="padding:8px 0 0">found via account partition scan (id mapping expired)</div>' : '');
  return false;
}

// One full message: the header fields, the metadata blob and the raw
// content. Shared by the admin dump box and the public share view, so
// both always show exactly the same thing.
function messageCardHtml(msg, j) {
  j = j || {};
  const fields = [['timestamp', msg.timestamp || msg.created_at], ['stored at', msg.created_at],
                  ['account', j.account], ['direction', msg.direction], ['contact', msg.contact],
                  ['content type', msg.content_type], ['state', msg.state],
                  ['disposition', (msg.disposition || []).join(', ') || null],
                  ['message id', j.message_id]];
  const rows = fields.filter(f => f[1]).map(f => `<tr style="cursor:default;border:0">
      <td style="padding:3px 18px 3px 0;font-size:13px;color:#475569;white-space:nowrap">${f[0]}</td>
      <td class="mono" style="padding:3px 0;font-size:13px;word-break:break-all">${esc(f[1])}</td>
    </tr>`).join('');
  // metadata is shown here, in the full message body, rather than as a
  // column in the day list — it is a JSON blob, too wide to tabulate.
  // Pretty-printed when it parses, verbatim when it does not.
  const metaBlock = msg.metadata ? `
    <div style="font-size:12px;color:#64748b;margin-top:10px">metadata</div>
    <pre style="font-size:12px;line-height:1.5;background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;margin:6px 0 0;white-space:pre-wrap;word-break:break-word">${esc(prettyJson(msg.metadata))}</pre>` : '';
  const body = contentBlock(msg.content);
  return `
  <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
    <table style="width:auto"><tbody>${rows}</tbody></table>${metaBlock}
    <div style="font-size:12px;color:#64748b;margin-top:10px">${body.label}</div>
    <pre style="font-size:12px;line-height:1.5;background:#f8fafc;border:1px solid var(--border);border-radius:8px;padding:12px;overflow:auto;margin:6px 0 0;white-space:pre-wrap;word-break:break-word">${esc(body.text)}</pre>
  </div>`;
}

// Message content is often a JSON payload (location shares, meet
// invitations, file-transfer descriptors, ...). Pretty-print it when it
// really parses as a JSON object or array, and say so in the label so it
// is clear the view is formatted rather than raw. Everything else — plain
// text, PGP blobs, bare numbers — is shown exactly as stored.
function contentBlock(content) {
  const text = content == null ? '' : String(content);
  if (text.trim()) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object')
        return { label: 'content (JSON)', text: JSON.stringify(parsed, null, 2) };
    } catch (e) {}
  }
  return { label: 'content', text: text };
}

// The public-link controls shown above a dumped message in the admin UI.
// The URL itself is not displayed — it is long and noisy — it lives in an
// off-screen field that the copy fallback can still select.
function shareLinkBoxHtml(account, id) {
  if (!account || !id) return '';
  const url = shareUrlFor(account, id);
  return `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="pbtn" type="button" onclick="copyShareUrl(this)" style="white-space:nowrap">Copy public link</button>
        <a class="pbtn" href="${esc(url)}" target="_blank" rel="noopener" style="white-space:nowrap;text-decoration:none">Open</a>
        <span style="font-size:12px;color:#64748b">anyone with the link can read this message without signing in</span>
      </div>
      <input id="shareUrl" readonly tabindex="-1" aria-hidden="true" value="${esc(url)}"
             style="position:fixed;left:-9999px;top:0;width:320px;height:28px;opacity:0">
    </div>`;
}

// Copy the share URL. navigator.clipboard needs a secure context (HTTPS or
// localhost); over plain HTTP fall back to selecting the off-screen field
// and letting execCommand do the copy.
function copyShareUrl(btn) {
  const input = $('shareUrl');
  if (!input) return;
  const done = () => {
    toast('Public link copied to clipboard');
    if (btn) {
      const label = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(() => { btn.textContent = label; }, 1500);
    }
  };
  const legacy = () => {
    try {
      input.select();
      input.setSelectionRange(0, input.value.length);
      if (document.execCommand('copy')) { done(); return; }
    } catch (e) {}
    toast('Could not copy the link automatically');
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(input.value).then(done, legacy);
  } else {
    legacy();
  }
}

/* ---------- public single-message view ---------- */

// Standalone page for '#messages?account=..&id=..'. Rendered instead of
// the dashboard or the login form, whether or not the visitor is signed
// in, so an admin sees exactly what the recipient of the link sees.
async function showSharedMessage(target) {
  stopLive();
  $('app').innerHTML = `
    <header class="topbar">
      <span class="dot"></span>
      <h1>SylkServer WebRTC Gateway — shared message</h1>
      <span class="spacer"></span>
    </header>
    <main>
      <div class="card" style="padding:20px" id="sharedBody">
        <div class="empty" style="padding:24px">Loading message…</div>
      </div>
      <div class="hint" style="padding:14px 4px 0">
        This is a read-only view of a single stored message, opened from a shared link.
        <a href="#messages" onclick="return clearShare()" style="color:var(--accent)">Go to the admin portal</a>
      </div>
    </main>`;
  const el = $('sharedBody');
  let j = null;
  try {
    const r = await api('public/message?account=' + encodeURIComponent(target.account) +
                        '&id=' + encodeURIComponent(target.id));
    j = await r.json();
  } catch (e) {}
  if (!j) {
    el.innerHTML = '<div class="err" style="padding:12px 4px">Could not load the message.</div>';
    return;
  }
  if (j.error) {
    el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`;
    return;
  }
  if (!j.found || !(j.messages || []).length) {
    el.innerHTML = `<div class="empty" style="padding:24px">
        This message is no longer available.<br>
        <span style="font-size:12px">It may have been deleted, or it expired — stored messages are kept for one year.</span>
      </div>`;
    return;
  }
  el.innerHTML = `<b style="font-size:15px">Message ${esc(j.message_id)}</b>` +
                 j.messages.map(msg => messageCardHtml(msg, j)).join('');
}

// Leave the shared view and load the normal admin portal.
function clearShare() {
  try { history.replaceState(null, '', '#messages'); } catch (e) {}
  boot();
  return false;
}

async function countMessages() {
  const btn = $('msgBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Counting…'; }
  let j = {};
  try {
    const r = await api('storage/messages');
    if (r.status === 403) { boot(); return; }
    j = await r.json();
  } catch (e) {}
  if ($('msgCount')) $('msgCount').textContent = j.total != null ? Number(j.total).toLocaleString() : '—';
  if (btn) { btn.disabled = false; btn.textContent = 'Count again'; }
  if (j.error) toast('Count failed: ' + j.error);
  else if (j.elapsed != null) toast('Counted in ' + j.elapsed + 's');
}

async function loadMedia() {
  const grid = $('mediaGrid');
  if (!grid) return;
  const r = await api('media');
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { grid.innerHTML = `<div class="card stat"><div class="err">Failed to load media usage.</div></div>`; return; }
  const defs = [
    ['file_transfers', 'File transfers'],
    ['filesharing', 'Conference files'],
    ['recordings', 'Recordings'],
  ];
  let total = 0, ok = false;
  grid.innerHTML = defs.map(([key, label]) => {
    const d = j[key] || {};
    if (d.error) return statTile(label, '—', [`<span class="err">${esc(d.error)}</span>`]);
    total += d.bytes || 0; ok = true;
    return statTile(label, fmtBytes(d.bytes), [
      d.files != null ? `${d.files} file${d.files === 1 ? '' : 's'} on disk` : null,
      d.path ? `<span class="mono" style="font-size:11px">${esc(d.path)}</span>` : null,
      d.note ? esc(d.note) : null,
    ]);
  }).join('');
  $('mediaTotal').textContent = ok ? fmtBytes(total) + ' total' : '—';
}

let ftFilters = { account: '', contact: '', type: '', sort: 'date', order: 'desc' };
const FT_TYPES = [['audio', 'Audio'], ['image', 'Images'], ['video', 'Video'], ['file', 'Files']];

async function loadFileTransfers(e) {
  if (e) e.preventDefault();
  const account = $('ftAcctInput').value.trim().toLowerCase();
  if (!account) return false;
  ftFilters = { account: account, contact: '', type: '', sort: 'date', order: 'desc' };
  const sel = $('ftContactSelect');
  if (sel) sel.innerHTML = '<option value="">All contacts</option>';
  const row = $('ftContactRow');
  if (row) row.style.display = 'none';
  ftFetch();
  return false;
}

function ftSetType(type) {
  ftFilters.type = type;
  ftFetch();
}

function ftSetContact(contact) {
  ftFilters.contact = contact;
  ftFetch();
}

function ftSetSort(sort) {
  ftFilters.sort = sort;
  ftFetch();
}

function ftToggleOrder() {
  ftFilters.order = ftFilters.order === 'asc' ? 'desc' : 'asc';
  ftFetch();
}

function ftCopyId(encoded) {
  const text = decodeURIComponent(encoded);
  const done = () => toast('Message id copied');
  const fail = () => toast('Copy failed');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => {
      ftCopyIdFallback(text) ? done() : fail();
    });
  } else {
    ftCopyIdFallback(text) ? done() : fail();
  }
}

function ftCopyIdFallback(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
  return ok;
}

async function ftDeleteTransfer(encId, encContact, btn) {
  const id = decodeURIComponent(encId);
  if (!confirm('Delete this file transfer — database entry and files?\n\n' + id)) return;
  btn.disabled = true;
  const r = await api('media/file-transfers/' + encodeURIComponent(ftFilters.account) + '/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transfer_id: id, contact: decodeURIComponent(encContact) }),
  });
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({}));
  if (j.ok) {
    toast(`Deleted ${j.deleted_messages} message${j.deleted_messages === 1 ? '' : 's'}, ${j.deleted_dirs} folder${j.deleted_dirs === 1 ? '' : 's'} (${fmtBytes(j.bytes)})`);
    ftFetch();
  } else { btn.disabled = false; toast('Delete failed: ' + (j.error || r.status)); }
}

async function ftDeleteOrphanFiles(count, btn) {
  if (!confirm(`Delete ${count} file-transfer folder${count === 1 ? '' : 's'} from disk that have no database entry?`)) return;
  btn.disabled = true;
  const r = await api('media/file-transfers/' + encodeURIComponent(ftFilters.account) + '/delete-orphan-files', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contact: ftFilters.contact }),
  });
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({}));
  if (j.ok) { toast(`Deleted ${j.deleted} folder${j.deleted === 1 ? '' : 's'} (${fmtBytes(j.bytes)})`); ftFetch(); }
  else { btn.disabled = false; toast('Delete failed: ' + (j.error || r.status)); }
}

async function ftPurgeOrphaned(count, btn) {
  if (!confirm(`Delete ${count} file-transfer message${count === 1 ? '' : 's'} whose files are no longer on disk?`)) return;
  btn.disabled = true;
  const r = await api('media/file-transfers/' + encodeURIComponent(ftFilters.account) + '/purge-orphaned', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contact: ftFilters.contact, type: ftFilters.type }),
  });
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => ({}));
  if (j.ok) { toast(`Purged ${j.deleted} message${j.deleted === 1 ? '' : 's'}`); ftFetch(); }
  else { btn.disabled = false; toast('Purge failed: ' + (j.error || r.status)); }
}

async function ftFetch() {
  const account = ftFilters.account;
  const el = $('ftResult');
  if (!el || !account) return;
  el.innerHTML = `<div class="empty" style="padding:18px">Scanning file transfers for ${esc(account)}${ftFilters.contact ? ' to contact ' + esc(ftFilters.contact) : ''}…</div>`;
  const params = [];
  if (ftFilters.contact) params.push('contact=' + encodeURIComponent(ftFilters.contact));
  if (ftFilters.type) params.push('type=' + encodeURIComponent(ftFilters.type));
  if (ftFilters.sort !== 'date') params.push('sort=' + encodeURIComponent(ftFilters.sort));
  if (ftFilters.order !== 'desc') params.push('order=' + encodeURIComponent(ftFilters.order));
  const r = await api('media/file-transfers/' + encodeURIComponent(account) + (params.length ? '?' + params.join('&') : ''));
  if (r.status === 403) { boot(); return; }
  const j = await r.json().catch(() => null);
  if (!j) { el.innerHTML = '<div class="err" style="padding:12px 4px">Lookup failed.</div>'; return; }
  if (j.error) { el.innerHTML = `<div class="err" style="padding:12px 4px">${esc(j.error)}</div>`; return; }
  const contacts = j.contacts || [];
  const row = $('ftContactRow');
  if (!contacts.length && !j.files) {
    if (row) row.style.display = 'none';
    el.innerHTML = `<div class="empty" style="padding:18px">No file-transfer messages in the database for ${esc(account)}.</div>`;
    return;
  }
  // account found — show the contact filter and fill its options
  const sel = $('ftContactSelect');
  if (sel) {
    sel.innerHTML = ['<option value="">All contacts</option>']
      .concat(contacts.map(c => `<option value="${esc(c.name)}"${c.name === j.contact ? ' selected' : ''}>${esc(c.name)} (${c.count})</option>`))
      .join('');
  }
  if (row) row.style.display = 'flex';
  // Drill-down chips: transfer counts by media type, scoped to the
  // selected contact (or the whole account when no contact is chosen).
  const byType = j.by_type || {};
  const allCount = FT_TYPES.reduce((s, t) => s + ((byType[t[0]] || {}).count || 0), 0);
  const allBytes = FT_TYPES.reduce((s, t) => s + ((byType[t[0]] || {}).bytes || 0), 0);
  const chips = [['', 'All', { count: allCount, bytes: allBytes }]]
    .concat(FT_TYPES.map(([key, label]) => [key, label, byType[key] || { count: 0, bytes: 0 }]))
    .concat([['missing', 'Missing file', byType.missing || { count: 0, bytes: 0 }],
             ['nodb', 'Missing database', byType.nodb || { count: 0, bytes: 0 }]])
    .map(([key, label, d]) => {
      const active = (j.type || '') === key;
      const style = active ? ' style="background:var(--accent);border-color:var(--accent);color:#fff"' : '';
      const sub = d.count ? `(${d.count} · ${fmtBytes(d.bytes)})` : '(0)';
      return `<button class="pbtn" onclick="ftSetType('${key}')"${style}>${label} <span style="opacity:.7;font-weight:500">${sub}</span></button>`;
    }).join('');
  const shown = (j.transfers || []).length;
  const typeLabel = j.type === 'missing' ? 'missing-file'
    : (FT_TYPES.find(t => t[0] === j.type) || [null, 'matching'])[1].toLowerCase();
  // a selected media category is implicit — hide the column (but keep
  // it for missing/nodb, where the media type still varies); same for
  // a selected contact
  const showType = !j.type || j.type === 'missing' || j.type === 'nodb';
  const showContact = !j.contact;
  const cols = 6 + (showType ? 1 : 0) + (showContact ? 1 : 0);
  // orphaned count within the current drill-down scope
  const orphaned = j.type === 'missing'
    ? ((byType.missing || {}).count || 0)
    : j.type
    ? ((byType[j.type] || {}).count || 0) - ((byType[j.type] || {}).on_disk || 0)
    : j.files - j.on_disk;
  const nodbCount = (byType.nodb || {}).count || 0;
  const rows = shown ? (j.transfers || []).map(t => `
    <tr style="cursor:default">
      <td class="mono" style="font-size:12px;color:#64748b;white-space:nowrap;padding:8px 16px 8px 0">${esc(t.date)}</td>
      <td class="mono" style="font-size:11px;color:#94a3b8;max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:8px 16px 8px 0;cursor:pointer" title="${esc(t.transfer_id)} — click to copy" onclick="ftCopyId('${encodeURIComponent(t.transfer_id || '')}')">${esc(t.transfer_id)}</td>
      ${showType ? `<td style="font-size:12px;color:#64748b;padding:8px 16px 8px 0">${esc(t.type)}</td>` : ''}
      ${showContact ? `<td class="mono" style="font-size:12px;padding:8px 16px 8px 0${(t.contact || '').includes('@') ? '' : ';color:#94a3b8;font-style:italic'}" title="${t.direction === 'outgoing' ? 'sent to' : t.direction === 'incoming' ? 'received from' : ''}">${t.direction === 'outgoing' ? '→' : t.direction === 'incoming' ? '←' : ''} ${esc(t.contact)}</td>` : ''}
      <td class="mono" style="font-size:12px;padding:8px 16px 8px 0;word-break:break-all${t.filename ? '' : ';color:#94a3b8;font-style:italic'}">${esc(t.filename || 'unknown (encrypted metadata)')}</td>
      <td class="mono" style="font-size:12px;text-align:right;white-space:nowrap;padding:8px 16px 8px 0">${fmtBytes(t.bytes)}</td>
      <td style="font-size:12px;white-space:nowrap;padding:8px 16px 8px 0">${t.in_db === false ? `<span style="color:#d97706">✓ ${fmtBytes(t.disk_bytes)} · no db entry</span>` : t.on_disk ? `<span style="color:var(--ok)">✓ ${fmtBytes(t.disk_bytes)}</span>` : '<span style="color:#dc2626">missing</span>'}</td>
      <td style="padding:8px 0;text-align:right"><button class="pbtn pbtn-kick" style="padding:3px 10px;font-size:12px"
          title="Delete this transfer: the database entry and its files on disk"
          onclick="ftDeleteTransfer('${encodeURIComponent(t.transfer_id || '')}','${encodeURIComponent(t.contact || '')}', this)">Delete</button></td>
    </tr>`).join('')
    : `<tr><td colspan="${cols}" class="empty">${j.type === 'nodb' ? 'No files without a database entry' : `No ${esc(j.type ? typeLabel : '')} transfers`}${j.contact ? ` for <span class="mono">${esc(j.contact)}</span>` : ''}.</td></tr>`;
  el.innerHTML = `
    <div style="margin-top:16px;border-top:1px solid var(--border);padding-top:14px">
      <b style="font-size:14px">${j.files} transfer${j.files === 1 ? '' : 's'} (${fmtBytes(j.bytes)}) in the database for <span class="mono">${esc(j.account)}</span>${j.contact ? ` → <span class="mono">${esc(j.contact)}</span>` : ''}</b>
      <div style="font-size:12px;color:#64748b;margin-top:4px">${j.on_disk} still on disk (${fmtBytes(j.disk_bytes)})${j.files > j.on_disk ? `, ${j.files - j.on_disk} expired or removed` : ''}</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px">
        ${chips}
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;padding:8px 0;border-top:1px solid var(--border)">
        ${j.type === 'nodb'
          ? `<button class="pbtn pbtn-kick" ${nodbCount ? '' : 'disabled'} onclick="ftDeleteOrphanFiles(${nodbCount}, this)"
                title="Delete the files on disk that have no file-transfer message in the database">Delete file${nodbCount === 1 ? '' : 's'} without database entry${nodbCount ? ` (${nodbCount})` : ''}</button>`
          : `<button class="pbtn pbtn-kick" ${orphaned ? '' : 'disabled'} onclick="ftPurgeOrphaned(${orphaned}, this)"
                title="Delete the file-transfer messages in the current view whose files are no longer on disk">Fix database for missing file${orphaned === 1 ? '' : 's'}${orphaned ? ` (${orphaned})` : ''}</button>`}
        <span style="flex:1"></span>
        <label style="font-size:12px;color:#64748b">Sort by</label>
        <select onchange="ftSetSort(this.value)" style="padding:6px 10px;border:1px solid var(--border);border-radius:7px;font-size:13px;background:#fff">
          <option value="date"${j.sort === 'date' ? ' selected' : ''}>Date</option>
          <option value="size"${j.sort === 'size' ? ' selected' : ''}>Size</option>
          <option value="name"${j.sort === 'name' ? ' selected' : ''}>Name</option>
        </select>
        <button class="pbtn" onclick="ftToggleOrder()" title="toggle sort order">${j.order === 'asc' ? '↑ ASC' : '↓ DESC'}</button>
      </div>
      ${j.total_transfers > shown ? `<div style="font-size:12px;color:#64748b;margin-top:4px">showing ${shown} of ${j.total_transfers} transfers</div>` : ''}
      <table style="margin-top:8px"><thead><tr>
        <th style="padding:6px 16px 4px 0">Date</th>
        <th style="padding:6px 16px 4px 0">Message id</th>
        ${showType ? '<th style="padding:6px 16px 4px 0">Type</th>' : ''}
        <th style="padding:6px 16px 4px 0">Contact</th>
        <th style="padding:6px 16px 4px 0">File</th>
        <th style="padding:6px 16px 4px 0;text-align:right">Size</th>
        <th style="padding:6px 16px 4px 0">On disk</th>
        <th style="padding:6px 0 4px"></th>
      </tr></thead><tbody>${rows}</tbody></table>
    </div>`;
}

let currentRoomUri = null;

async function openRoom(encUri, displayUri) {
  currentRoom = encUri;
  currentRoomUri = displayUri;
  $('d-uri').textContent = displayUri;
  $('d-sub').textContent = 'Loading participants…';
  $('d-body').innerHTML = '';
  meterEls = {}; lastPartSig = null;
  $('overlay').classList.add('open');
  await renderParticipants();
}

let meterEls = {};   // audio_pid -> { fill, num, row }
let pidByTarget = {}; // for reference
let lastPartSig = null;

async function renderParticipants() {
  const encUri = currentRoom;
  const r = await api('rooms/' + encUri + '/participants');
  if (r.status === 403) { closeDrawer(); boot(); return; }
  if (r.status === 404) { $('d-sub').textContent = 'Room no longer exists.'; $('d-body').innerHTML = ''; lastPartSig = null; return; }
  const j = await r.json().catch(() => ({ participants: [] }));
  const ps = j.participants || [];
  $('d-sub').textContent = 'Janus room ' + (j.janus_room_id != null ? j.janus_room_id : '?') +
                           ' · ' + ps.length + (ps.length === 1 ? ' participant' : ' participants');
  // Only rebuild the DOM when the roster actually changes — otherwise the
  // 7s refresh would flash the table and reset the live meters. The audio
  // loop updates meters in place between rebuilds.
  const sig = JSON.stringify(ps.map(p => [p.kind, p.target_id, p.muted, p.display_name, p.audio_pid]));
  if (sig === lastPartSig && Object.keys(meterEls).length) return;
  lastPartSig = sig;

  if (!ps.length) {
    $('d-body').innerHTML = `<div class="empty">No participants in this conference.</div>`;
    meterEls = {};
    return;
  }
  $('d-body').innerHTML = `
    <table>
      <thead><tr>
        <th>Participant</th><th class="lvl-cell">Audio level</th><th style="text-align:right">Actions</th>
      </tr></thead>
      <tbody>
        ${ps.map((p, i) => {
          const tid = encodeURIComponent(p.target_id == null ? '' : p.target_id);
          const muteLabel = p.muted === true ? 'Unmute' : 'Mute';
          const muteVal = p.muted === true ? 'false' : 'true';
          const isBridge = p.kind === 'bridge';
          const badge = p.kind === 'sip'
            ? '<span class="badge badge-sip">SIP</span>'
            : p.kind === 'bridge'
            ? '<span class="badge badge-bridge">Bridge</span>'
            : '<span class="badge badge-webrtc">WebRTC</span>';
          const mutedBadge = p.muted === true ? '<span class="badge badge-muted">Muted</span>' : '';
          const srcTitle = p.kind === 'sip' ? 'mic level from conference focus (0–255)' : 'speaker activity from Janus (talking + dBov)';
          const uri = stripSip(p.uri);
          const name = p.display_name || uri || 'unknown';
          const slow = (p.slow_download ? ' <span style="color:#dc2626;font-size:11px">↓slow</span>' : '')
                     + (p.slow_upload ? ' <span style="color:#dc2626;font-size:11px">↑slow</span>' : '');
          const ua = p.user_agent ? `<div style="font-size:11px;color:#94a3b8;margin-top:1px">${esc(p.user_agent)}${slow}</div>` : (slow ? `<div style="margin-top:1px">${slow}</div>` : '');
          return `
          <tr style="cursor:default" data-row="${i}">
            <td>
              <div style="font-weight:600">${esc(name)} ${badge}${mutedBadge}<span class="spkdot" data-dot="${i}"></span></div>
              <div class="mono" style="font-size:12px;color:#64748b">${esc(uri)}</div>
              ${ua}
            </td>
            <td>
              ${isBridge ? '<span style="font-size:12px;color:#94a3b8">audio mixer</span>'
                         : `<div class="meter" title="${srcTitle}"><i data-fill="${i}"></i></div>
              <div class="lvl-num" data-num="${i}">—</div>`}
            </td>
            <td>
              ${isBridge ? '' : `<div class="actions">
                <button class="pbtn" onclick="muteParticipant('${tid}', ${muteVal}, this)">${muteLabel}</button>
                <button class="pbtn pbtn-kick" onclick="kickParticipant('${tid}','${esc(name)}', this)">Kick</button>
              </div>`}
            </td>
          </tr>`; }).join('')}
      </tbody>
    </table>
    <div class="hint">Audio meter: SIP callers show a continuous mic level from the conference focus; WebRTC participants show speaker activity from Janus (talking + dBov), which updates on talking transitions rather than continuously. WebRTC mute asks the client to mute at source; SIP mute/kick is proxied to the focus.</div>`;
  // Index meter elements by target_id (the key the audio-levels endpoint
  // returns) for the live-update loop.
  meterEls = {};
  ps.forEach((p, i) => {
    if (p.target_id == null) return;
    meterEls[String(p.target_id)] = {
      fill: $('d-body').querySelector(`[data-fill="${i}"]`),
      num: $('d-body').querySelector(`[data-num="${i}"]`),
      row: $('d-body').querySelector(`[data-row="${i}"]`),
      dot: $('d-body').querySelector(`[data-dot="${i}"]`),
    };
  });
}

async function updateAudioLevels() {
  if (!currentRoom || !$('overlay').classList.contains('open')) return;
  if (!Object.keys(meterEls).length) return;
  let j;
  try {
    const r = await api('rooms/' + currentRoom + '/audio-levels');
    if (!r.ok) return;
    j = await r.json();
  } catch (e) { return; }
  const scale = j.scale || 255;
  const levels = j.levels || {};
  for (const tid in meterEls) {
    const el = meterEls[tid];
    if (!el.fill) continue;
    const v = levels[tid] || {};
    const value = v.value || 0;
    const stale = !!v.stale;
    const talking = !!v.talking;
    const pct = Math.min(100, Math.round((value / scale) * 100));
    el.fill.style.width = pct + '%';
    el.fill.parentElement.classList.toggle('stale', stale);
    if (el.num) el.num.textContent = stale ? '—' : String(value);
    if (el.row) el.row.classList.toggle('speaking', talking);
  }
}

async function muteParticipant(tid, muted, btn) {
  btn.disabled = true;
  const r = await api('rooms/' + currentRoom + '/participants/' + tid + '/mute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ muted: muted }),
  });
  if (r.ok) { toast(muted ? 'Mute sent' : 'Unmute sent'); setTimeout(renderParticipants, 1200); }
  else { btn.disabled = false; toast('Action failed (' + r.status + ')'); }
}

async function kickParticipant(tid, label, btn) {
  if (!confirm('Remove ' + label + ' from this conference?')) return;
  btn.disabled = true;
  const r = await api('rooms/' + currentRoom + '/participants/' + tid + '/kick', { method: 'POST' });
  if (r.ok) { toast('Kick sent'); setTimeout(renderParticipants, 1200); }
  else { btn.disabled = false; toast('Action failed (' + r.status + ')'); }
}

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

function closeDrawer() { $('overlay').classList.remove('open'); currentRoom = null; currentRoomUri = null; meterEls = {}; lastPartSig = null; }
$('overlay').addEventListener('click', (e) => { if (e.target.id === 'overlay') closeDrawer(); });

/* ---------- live updates ---------- */
function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; loadRooms(); }, 400);
}
function startLive() {
  stopLive();
  try {
    evtSource = new EventSource('rooms/events');
    evtSource.onmessage = () => scheduleRefresh();   // room created/destroyed
    evtSource.onerror = () => {};                     // browser auto-reconnects
  } catch (e) {}
  // Poll for room/session/end-point changes (none of these emit SSE
  // events). Only the visible section is refreshed; switching tabs
  // triggers an immediate load. The Storage tab is intentionally not
  // polled — its backend scans are heavier, so it loads on demand.
  pollTimer = setInterval(() => {
    if (currentView === 'endpoints') loadEndpoints();
    else if (currentView === 'sessions') loadSessions();
    else if (currentView === 'conferences') loadRooms();
    if (currentRoom) renderParticipants();
  }, 7000);
  // Fast loop for live audio meters while a room panel is open.
  audioTimer = setInterval(updateAudioLevels, 350);
}
function stopLive() {
  if (evtSource) { evtSource.close(); evtSource = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (audioTimer) { clearInterval(audioTimer); audioTimer = null; }
  if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }
}

async function logout() {
  await api('logout', { method: 'POST' });
  closeDrawer();
  boot();
}

/* ---------- bootstrap ---------- */

// account+id currently rendered by the public share view, so a hash
// change can tell 'still the same shared message' from 'something else'.
let shareKey = null;

async function boot() {
  // A share link wins over both the dashboard and the login form: the
  // public message view needs no session at all.
  const shared = sharedMessageTarget();
  shareKey = shared ? shared.account + ' | ' + shared.id : null;
  if (shared) { showSharedMessage(shared); return; }
  const r = await api('session');
  const j = await r.json().catch(() => ({ authenticated: false }));
  if (j.authenticated) showDashboard(j.username);
  else showLogin(j.login_configured !== false);
}

// Re-render when the hash is changed from outside the app (pasting a
// share link into the address bar of an already open tab, Back/Forward).
// switchView() uses replaceState, which does not fire this event.
window.addEventListener('hashchange', () => {
  const shared = sharedMessageTarget();
  const key = shared ? shared.account + ' | ' + shared.id : null;
  if (key !== shareKey) boot();
});

boot();
