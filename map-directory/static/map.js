// The station map page. Served from /static/map.js under a strict CSP:
// nothing inline, no eval, data only from this host.
(function () {
  const map = L.map('map', { worldCopyJump: true }).setView([36, -96], 4);
  // OpenStreetMap's own tiles, darkened in CSS (.dark-tiles). CARTO's
  // free basemaps started watermarking "API KEY REQUIRED" on 2026-09-13;
  // OSM needs no key. Keep the load light: one small site, no prefetch.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    className: 'dark-tiles',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  const cluster = L.markerClusterGroup({ maxClusterRadius: 40, showCoverageOnHover: false });
  map.addLayer(cluster);
  const focus = new URLSearchParams(location.search).get('station');
  let focusMarker = null;
  // The last listing, kept so switching metric relabels the pins already
  // on the map instead of refetching the directory.
  let features = [];
  // The station whose popup is open, so relabelling the pins can put it
  // back: switching metric rebuilds every marker, and without this the
  // card the reader was reading vanished under them.
  let openStation = null;

  // The pin metric (2.3, Volney's ask): Weather Underground shows a
  // temperature pin and nothing else, so the reading people actually came
  // for — the rain total, the gust, the feels-like — is buried one tap
  // deep in a card. Every one of these already rides the beacon, so the
  // toggle costs no request and no server change: the pins simply relabel.
  //
  // Each metric owns its OWN colour bands, the way the app's Theme does.
  // Rain is not "hot" at 2 inches and a 70° dew point is not "warm", it is
  // oppressive — one shared intensity ramp would say the wrong thing about
  // half of them. `bands` reads low to high: the first threshold the value
  // falls under wins, and the bare last entry is everything above.
  // Keep in step with MapMetric in the app's StationMapView.swift.
  const METRICS = [
    { id: 'temp', label: 'Temp', field: 'tempf', unit: '°F',
      fmt: v => Math.round(v) + '°',
      bands: [[32, 't-freeze'], [50, 't-cold'], [70, 't-cool'], [85, 't-mild'], [100, 't-warm'], 't-hot'] },
    { id: 'feels', label: 'Feels', field: 'feelsLike', unit: '°F',
      fmt: v => Math.round(v) + '°',
      bands: [[32, 't-freeze'], [50, 't-cold'], [70, 't-cool'], [85, 't-mild'], [100, 't-warm'], 't-hot'] },
    { id: 'rain', label: 'Rain today', field: 'dailyrainin', unit: 'in',
      fmt: v => v.toFixed(2),
      bands: [[0.01, 'r-dry'], [0.1, 'r-trace'], [0.5, 'r-light'], [1, 'r-mod'], 'r-heavy'] },
    { id: 'rate', label: 'Rain rate', field: 'hourlyrainin', unit: 'in/hr',
      fmt: v => v.toFixed(2),
      bands: [[0.01, 'r-dry'], [0.1, 'r-trace'], [0.3, 'r-light'], [1, 'r-mod'], 'r-heavy'] },
    { id: 'wind', label: 'Wind', field: 'windspeedmph', unit: 'mph',
      fmt: v => String(Math.round(v)),
      bands: [[8, 'w-calm'], [20, 'w-breezy'], [35, 'w-windy'], [50, 'w-strong'], 'w-gale'] },
    { id: 'gust', label: 'Gust', field: 'windgustmph', unit: 'mph',
      fmt: v => String(Math.round(v)),
      bands: [[8, 'w-calm'], [20, 'w-breezy'], [35, 'w-windy'], [50, 'w-strong'], 'w-gale'] },
    { id: 'humidity', label: 'Humidity', field: 'humidity', unit: '%',
      fmt: v => Math.round(v) + '%',
      bands: [[25, 'h-dry'], [60, 'h-ok'], [80, 'h-humid'], 'h-sat'] },
    { id: 'dew', label: 'Dew point', field: 'dewPoint', unit: '°F',
      fmt: v => Math.round(v) + '°',
      bands: [[55, 'd-dry'], [60, 'd-ok'], [65, 'd-sticky'], [70, 'd-muggy'], [75, 'd-oppressive'], 'd-severe'] },
    { id: 'pressure', label: 'Pressure', field: 'baromrelin', unit: 'inHg',
      fmt: v => v.toFixed(2),
      bands: [[29.6, 'p-low'], [30.2, 'p-normal'], 'p-high'] },
    { id: 'uv', label: 'UV', field: 'uv', unit: 'index',
      fmt: v => String(Math.round(v)),
      bands: [[3, 'u-low'], [6, 'u-mod'], [8, 'u-high'], [11, 'u-vhigh'], 'u-extreme'] },
  ];
  const STORE_KEY = 'zasder.map.metric';

  function metricById(id) { return METRICS.find(m => m.id === id) || METRICS[0]; }

  // Private browsing and blocked site data make localStorage throw on
  // access, not just return null — the map must still open.
  function savedMetric() {
    try { return metricById(localStorage.getItem(STORE_KEY)); }
    catch (e) { return METRICS[0]; }
  }
  function saveMetric(id) {
    try { localStorage.setItem(STORE_KEY, id); } catch (e) { /* not fatal */ }
  }

  let metric = savedMetric();

  // The pin class for a value under this metric's bands. A station that
  // does not measure the reading gets the neutral pin, never a zero —
  // absent is not zero, on the map as everywhere else.
  function pinClass(m, v) {
    if (v == null || !isFinite(v)) return 't-none';
    for (const b of m.bands) {
      if (!Array.isArray(b)) return b;
      if (v < b[0]) return b[1];
    }
    return 't-none';
  }
  function pinLabel(m, v) {
    return (v == null || !isFinite(v)) ? '·' : m.fmt(v);
  }

  function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  // A reading as a finite number, or null. The directory stores every
  // reading as a float, but the page must not depend on that: one string
  // in one beacon once reached `.toFixed` here, the render threw, and the
  // whole map read "unavailable" for every visitor until the beacon
  // expired. Whatever arrives, one bad value blanks one row, never the map.
  function reading(c, k) {
    if (c == null || c[k] == null) return null;
    const n = Number(c[k]);
    return Number.isFinite(n) ? n : null;
  }
  function age(ms) {
    const m = Math.round((Date.now() - ms) / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return m + ' min ago';
    const h = Math.round(m / 60);
    return h + (h === 1 ? ' hour ago' : ' hours ago');
  }
  function row(k, v) { return v == null ? '' : '<tr><td class="k">' + k + '</td><td>' + v + '</td></tr>'; }
  function card(p) {
    const c = p.conditions || {};
    const name = p.name ? esc(p.name) : 'A shared station';
    let html = '<div class="card"><h3>' + name + '</h3>';
    const where = p.precision === 'city' ? 'Somewhere in the area · ' : (p.fuzzed ? 'Approximate location · ' : '');
    html += '<div class="muted">' + where + esc(age(p.observed_ms || p.sent_ms)) + '</div>';
    html += '<table>';
    const n = k => reading(c, k);
    const temp = n('tempf'), feels = n('feelsLike'), hum = n('humidity'), dew = n('dewPoint');
    const wind = n('windspeedmph'), gust = n('windgustmph'), pres = n('baromrelin');
    const rain = n('dailyrainin'), rate = n('hourlyrainin'), uv = n('uv');
    html += row('Temp', temp != null ? Math.round(temp) + '°F' : null);
    html += row('Feels', feels != null ? Math.round(feels) + '°F' : null);
    html += row('Humidity', hum != null ? Math.round(hum) + '%' : null);
    html += row('Dew point', dew != null ? Math.round(dew) + '°F' : null);
    html += row('Wind', wind != null ? Math.round(wind) + ' mph' + (gust != null ? ' · gust ' + Math.round(gust) : '') : null);
    html += row('Pressure', pres != null ? pres.toFixed(2) + ' inHg' : null);
    html += row('Rain today', rain != null ? rain.toFixed(2) + ' in' : null);
    html += row('Rain rate', rate != null ? rate.toFixed(2) + ' in/hr' : null);
    html += row('UV', uv != null ? Math.round(uv) : null);
    html += '</table>';
    if (p.visit_url && /^https:\/\//.test(p.visit_url)) {
      html += '<p><a href="' + esc(p.visit_url) + '" rel="noopener nofollow" target="_blank">Visit</a></p>';
    }
    // The station's public id, for owners who keep their server's own
    // address off the map. It is the name they can give out.
    if (p.public_id) {
      html += '<div class="muted id">' + esc(p.public_id) + '</div>';
    }
    html += '</div>';
    return html;
  }

  // MARK: the metric chips

  function buildChips() {
    const host = document.getElementById('metrics');
    if (!host) return;
    for (const m of METRICS) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip' + (m.id === metric.id ? ' on' : '');
      b.textContent = m.label;
      b.setAttribute('aria-pressed', m.id === metric.id ? 'true' : 'false');
      b.addEventListener('click', () => selectMetric(m.id));
      host.appendChild(b);
    }
  }
  function selectMetric(id) {
    if (id === metric.id) return;
    metric = metricById(id);
    saveMetric(metric.id);
    const host = document.getElementById('metrics');
    if (host) {
      const kids = host.children;
      for (let i = 0; i < kids.length; i++) {
        const on = METRICS[i] && METRICS[i].id === metric.id;
        kids[i].className = 'chip' + (on ? ' on' : '');
        kids[i].setAttribute('aria-pressed', on ? 'true' : 'false');
      }
    }
    render();
  }

  // MARK: rendering

  /// Redraws every pin from the last listing. Reopens the card that was
  /// open, so relabelling the map does not snatch away what the reader was
  /// reading.
  function render() {
    cluster.clearLayers();
    focusMarker = null;
    let reopen = null;
    let measuring = 0;
    for (const f of features) {
      const p = f.properties; const [lon, lat] = f.geometry.coordinates;
      const v = reading(p.conditions || {}, metric.field);
      if (v != null) measuring++;
      const text = pinLabel(metric, v);
      // A 34px circle holds "98°" comfortably and "29.92" not at all, so
      // the longer readings (pressure, rain to two decimals) step down a
      // size rather than spilling over the edge of the pin.
      const fit = text.length >= 5 ? ' tiny' : (text.length === 4 ? ' small' : '');
      const icon = L.divIcon({ className: '', iconSize: [34, 34], iconAnchor: [17, 17],
        html: '<div class="pin ' + pinClass(metric, v) + fit + '">' + esc(text) + '</div>' });
      const m = L.marker([lat, lon], { icon, title: p.name || 'Shared station' });
      m.bindPopup(card(p), { maxWidth: 280 });
      m.on('popupopen', () => { openStation = p.station_id; });
      m.on('popupclose', () => { if (openStation === p.station_id) openStation = null; });
      cluster.addLayer(m);
      if (focus && p.station_id === focus) { focusMarker = m; }
      if (openStation && p.station_id === openStation) { reopen = m; }
    }
    if (reopen) { cluster.zoomToShowLayer(reopen, () => reopen.openPopup()); }
    const total = features.length;
    let line = total + (total === 1 ? ' station' : ' stations') + ' sharing';
    // Say what the pins are showing, and say so honestly when some
    // stations do not measure it — a blank pin is a missing sensor, not a
    // zero, and the count is how a reader tells the two apart.
    line += ' · ' + metric.label.toLowerCase() + ' (' + metric.unit + ')';
    if (total > 0 && measuring < total) {
      line += ' · ' + (total - measuring) + ' not measuring it';
    }
    const el = document.getElementById('count');
    if (el) el.textContent = line;
  }

  async function load() {
    try {
      const r = await fetch('/v1/beacons', { cache: 'no-store' });
      const geo = await r.json();
      features = (geo.features || []).filter(
        f => f && f.properties && f.geometry && Array.isArray(f.geometry.coordinates)
             && f.geometry.coordinates.length === 2);
      render();
      if (focusMarker && !load.fitted) {
        // A share link (?station=<id>): open on that station.
        map.setView(focusMarker.getLatLng(), 11);
        cluster.zoomToShowLayer(focusMarker, () => focusMarker.openPopup());
        load.fitted = true;
      } else if (features.length > 0 && !load.fitted) { map.fitBounds(cluster.getBounds().pad(0.2)); load.fitted = true; }
    } catch (e) {
      document.getElementById('count').textContent = 'map unavailable';
    }
  }
  buildChips();
  load();
  setInterval(load, 5 * 60 * 1000);
})();
