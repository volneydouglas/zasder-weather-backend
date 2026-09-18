// The operator page: list servers, block, unblock, forget. No inline
// script (CSP). The token lives in sessionStorage for this tab only.
(function () {
  const $ = (s) => document.querySelector(s);
  const tbody = $('#servers tbody');
  const status = $('#status');
  let token = sessionStorage.getItem('map-admin-token') || '';
  if (token) $('#token').value = token;

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function when(ms) { return ms ? new Date(ms).toLocaleString() : ''; }

  async function call(path, method) {
    const r = await fetch(path, { method, headers: { 'Authorization': 'Bearer ' + token }, cache: 'no-store' });
    if (r.status === 401 || r.status === 403) throw new Error('bad token');
    if (r.status === 429) throw new Error('too many bad tokens, wait a minute');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function button(label, cls, onclick) {
    const b = document.createElement('button'); b.textContent = label; if (cls) b.className = cls;
    b.addEventListener('click', onclick); return b;
  }

  async function load() {
    status.textContent = 'loading…';
    try {
      const data = await call('/v1/admin/servers', 'GET');
      tbody.textContent = '';
      for (const s of data.servers) {
        const tr = document.createElement('tr'); if (s.blocked) tr.className = 'blocked';
        tr.innerHTML = '<td>' + esc(s.server_id) + '</td><td>' + s.live + '</td><td>' + esc(s.names) + '</td>'
          + '<td>' + esc(when(s.first_ms)) + '</td><td>' + esc(when(s.last_ms)) + '</td>'
          + '<td class="key">' + esc(s.pubkey.slice(0, 16)) + '…</td>'
          + '<td><span class="pill ' + (s.blocked ? 'off' : 'on') + '">' + (s.blocked ? 'blocked' : 'listed') + '</span></td><td></td>';
        const actions = tr.lastElementChild;
        if (s.blocked) {
          actions.appendChild(button('Unblock', '', () => act('/v1/admin/servers/' + encodeURIComponent(s.server_id) + '/unblock', 'POST')));
        } else {
          actions.appendChild(button('Block', 'danger', () => act('/v1/admin/servers/' + encodeURIComponent(s.server_id) + '/block', 'POST')));
        }
        actions.appendChild(document.createTextNode(' '));
        actions.appendChild(button('Forget', 'danger', () => act('/v1/admin/servers/' + encodeURIComponent(s.server_id), 'DELETE')));
        tbody.appendChild(tr);
      }
      $('#servers').hidden = false;
      status.textContent = data.servers.length + ' server(s)';
    } catch (e) {
      status.textContent = e.message;
    }
  }

  async function act(path, method) {
    status.textContent = 'working…';
    try { await call(path, method); await load(); }
    catch (e) { status.textContent = e.message; }
  }

  $('#auth').addEventListener('submit', (ev) => {
    ev.preventDefault();
    token = $('#token').value.trim();
    try { sessionStorage.setItem('map-admin-token', token); } catch (e) {}
    load();
  });
  if (token) load();
})();
