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
  // The longitude the pins are drawn around after a Near me tap (see
  // frameNear), so a view across the date line shows its pins. Null until
  // then: the pins keep their own longitudes.
  let wrapRef = null;
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
    // 2.5 (C14): a city pin names its town; a pin the owner placed says so.
    const where = p.placed_by === 'owner' ? 'Location set by the owner · '
      : p.precision === 'city' ? (p.place ? 'In ' + esc(p.place) + ' · ' : 'Somewhere in the area · ')
      : (p.fuzzed ? 'Approximate location · ' : '');
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
      // After a Near me framed the view across the date line, a pin draws
      // in the same world copy as that view, or it lands 360° away, off
      // screen (CodeRabbit, PR #123). wrapRef is null otherwise.
      const drawLon = wrapRef == null ? lon : unwrap(lon, wrapRef);
      const m = L.marker([lat, drawLon], { icon, title: p.name || 'Shared station' });
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
      load.listed = true;
      if (nearHere) {
        // A Near me tap that beat the first listing: frame it now.
        frameNear();
      } else if (focusMarker && !load.fitted) {
        // A share link (?station=<id>): open on that station.
        map.setView(focusMarker.getLatLng(), 11);
        cluster.zoomToShowLayer(focusMarker, () => focusMarker.openPopup());
        load.fitted = true;
      } else if (features.length > 0 && !load.fitted) { map.fitBounds(cluster.getBounds().pad(0.2)); load.fitted = true; }
    } catch (e) {
      document.getElementById('count').textContent = 'map unavailable';
    }
  }
  // MARK: near me (2.8)

  // "Near me": zoom to the stations around the visitor. Twin of NearMe in
  // the app's StationMapView.swift (the same 3 stations, the same 50 mi).
  // The browser asks for a position ONLY when the button is tapped, never
  // on page load, and this page NEVER SENDS THE POSITION: it is not
  // fetched with, not put in the URL, not stored, not logged. It moves the
  // map and draws one dot, and that is all. Like any map view, the tiles
  // for the area then on screen load from OpenStreetMap, so the tile server
  // sees which area is shown, as it does for every pan. The directory's
  // Permissions-Policy allows geolocation for this origin alone
  // (app/main.py).
  const NEAR_STATIONS = 3;        // the nearest few frame the view
  const NEAR_MAX_KM = 80.47;      // 50 mi: farther than this is not "near"
  const NEAR_ALONE_ZOOM = 10;     // nothing near: about 40 km across
  let meMarker = null;
  // The last fix, kept only in this variable so a tap that lands before
  // the first listing can be framed when the pins arrive (CodeRabbit,
  // PR #123). Cleared once framed.
  let nearHere = null;
  // Counts Near me taps; a position or error for an older tap is dropped.
  let nearSeq = 0;

  // Great-circle distance in km. Enough for "which pins are closest".
  function km(aLat, aLon, bLat, bLon) {
    const r = Math.PI / 180;
    const dLat = (bLat - aLat) * r, dLon = (bLon - aLon) * r;
    const h = Math.sin(dLat / 2) ** 2
      + Math.cos(aLat * r) * Math.cos(bLat * r) * Math.sin(dLon / 2) ** 2;
    return 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  // A longitude moved by whole turns to within 180° of `ref`, so a box
  // across the date line spans the short way round.
  function unwrap(lon, ref) {
    while (lon - ref > 180) lon -= 360;
    while (lon - ref < -180) lon += 360;
    return lon;
  }

  // The nearest few stations within NEAR_MAX_KM, as [lat, lon] pairs.
  function nearestStations(lat, lon) {
    return features
      .map(f => { const [fLon, fLat] = f.geometry.coordinates;
                  return { ll: [fLat, unwrap(fLon, lon)], d: km(lat, lon, fLat, fLon) }; })
      .filter(x => Number.isFinite(x.d) && x.d <= NEAR_MAX_KM)
      .sort((a, b) => a.d - b.d)
      .slice(0, NEAR_STATIONS)
      .map(x => x.ll);
  }

  function nearNote(text) {
    const el = document.getElementById('nearme-note');
    if (!el) return;
    // Never `hidden`: a live region that appears with its message may not
    // announce it (CodeRabbit, PR #123). Empty text hides the box in CSS.
    el.textContent = text || '';
  }

  // Frames the last fix with the nearest stations in the current listing.
  function frameNear() {
    if (!nearHere) return;
    const here = nearHere;
    nearHere = null;
    const near = nearestStations(here[0], here[1]);
    // Redraw the pins around the reader's longitude: the fit below is
    // measured the same way, so a neighbour across the date line is drawn
    // inside it.
    wrapRef = here[1];
    render();
    if (near.length) {
      nearNote('');
      map.fitBounds(L.latLngBounds([here].concat(near)).pad(0.3), { maxZoom: 12 });
    } else {
      map.setView(here, NEAR_ALONE_ZOOM);
      nearNote('No shared stations within 50 mi (80 km) yet.');
    }
  }

  function nearMe() {
    const btn = document.getElementById('nearme');
    if (!('geolocation' in navigator)) {
      nearNote('This browser cannot share a location.');
      return;
    }
    nearNote('');
    // A new ask forgets the last answer: a failed tap must not leave an old
    // dot up, nor a pending frame a later listing would act on (CodeRabbit,
    // PR #123).
    if (meMarker) { map.removeLayer(meMarker); meMarker = null; }
    nearHere = null;
    // Each tap is its own request; only the latest may touch the dot, the
    // map, the note or the busy state. Two quick taps otherwise let the
    // first answer land after the second ask and put the old fix back
    // (CodeRabbit, PR #123).
    const seq = ++nearSeq;
    if (btn) btn.setAttribute('aria-busy', 'true');
    navigator.geolocation.getCurrentPosition(pos => {
      if (seq !== nearSeq) return;
      if (btn) btn.removeAttribute('aria-busy');
      const lat = pos.coords.latitude, lon = pos.coords.longitude;
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        nearNote('Your location could not be read.');
        return;
      }
      const here = [lat, lon];
      if (meMarker) { meMarker.setLatLng(here); }
      else {
        meMarker = L.marker(here, { interactive: false, keyboard: false, zIndexOffset: 1000,
          icon: L.divIcon({ className: '', iconSize: [20, 20], iconAnchor: [10, 10],
                            html: '<div class="me" aria-hidden="true"></div>' }) }).addTo(map);
      }
      // A tap is an explicit ask, so it wins over a ?station= link and
      // over the first listing's fit (load.fitted).
      load.fitted = true;
      nearHere = here;
      if (load.listed) {
        frameNear();
      } else {
        // The pins are still loading: show the reader now, frame the
        // neighbours when load() lands. Never "nobody near" before then.
        map.setView(here, NEAR_ALONE_ZOOM);
      }
    }, err => {
      if (seq !== nearSeq) return;
      if (btn) btn.removeAttribute('aria-busy');
      nearNote(err && err.code === 1
        ? 'Location is off for this page. Allow it in the browser to see stations near you.'
        : 'Your location could not be found. Try again in a moment.');
    }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 5 * 60 * 1000 });
  }

  const nearBtn = document.getElementById('nearme');
  if (nearBtn) nearBtn.addEventListener('click', nearMe);

  buildChips();
  load();
  setInterval(load, 5 * 60 * 1000);
})();
