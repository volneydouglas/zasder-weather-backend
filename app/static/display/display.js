/* Zasder Weather station display (2.8). Draws /display/data (public mode)
   or /api/display (a view-only token from the URL fragment) on a 1280x800
   stage scaled to the screen. No third-party code, no telemetry: the only
   requests are to this server. */
(function () {
  "use strict";
  var REFRESH_MS = 30000;
  var TOKEN_KEY = "zasder.display.token";
  var $ = function (id) { return document.getElementById(id); };
  var D = null, sel = 0, lastSig = {}, rotateTimer = null, refreshTimer = null;

  // ── token: read once from the fragment, keep for the kiosk, strip ────
  function readToken() {
    var h = (location.hash || "").replace(/^#/, "");
    var tok = "";
    if (h) {
      var m = h.match(/(?:^|[&;])t=([A-Za-z0-9_]+)/);
      tok = m ? m[1] : (/^[A-Za-z0-9_]{16,}$/.test(h) ? h : "");
      // The fragment never reaches the server, and it should not linger
      // in history or a screenshot of the address bar either.
      try { history.replaceState(null, "", location.pathname + location.search); } catch (e) {}
    }
    try {
      if (tok) localStorage.setItem(TOKEN_KEY, tok);
      else tok = localStorage.getItem(TOKEN_KEY) || "";
    } catch (e) {}
    return tok;
  }
  function forgetToken() { try { localStorage.removeItem(TOKEN_KEY); } catch (e) {} }
  // A refused link is terminal: keep its explanation on screen instead of
  // falling back to the anonymous feed on the next poll (CodeRabbit, #129).
  function stopRefresh() { if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; } }

  var token = readToken();
  var params = new URLSearchParams(location.search);
  var startStation = params.get("station");
  var rotateS = Math.max(15, parseInt(params.get("rotate") || "0", 10) || 0);
  if (!params.has("rotate")) rotateS = 0;
  if (params.has("framed")) $("stage").classList.add("framed");

  // ── stage scaling: 1280x800, fit the screen, never scroll ────────────
  function fit() {
    var s = Math.min(window.innerWidth / 1280, window.innerHeight / 800);
    $("stage").style.transform = "scale(" + s + ")";
  }
  window.addEventListener("resize", fit);
  fit();

  // ── idle pointer: a wall display's mouse parks over a reading forever ─
  var IDLE_MS = 3000, idleTimer = null;
  function wake() {
    document.documentElement.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(function () {
      document.documentElement.classList.add("idle");
    }, IDLE_MS);
  }
  ["mousemove", "mousedown", "touchstart"].forEach(function (ev) {
    window.addEventListener(ev, wake, { passive: true });
  });
  wake();

  // ── formatting (values arrive in °F, mph, inHg, in; null = absent) ───
  var tz = undefined;
  function fmtTime(ms) {
    try { return new Date(ms).toLocaleTimeString("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit" }); }
    catch (e) { return new Date(ms).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }); }
  }
  var has = function (v) { return v !== null && v !== undefined && isFinite(v); };
  var r0 = function (v) { return has(v) ? String(Math.round(v)) : "—"; };
  var f1 = function (v) { return has(v) ? (+v).toFixed(1) : "—"; };
  var f2 = function (v) { return has(v) ? (+v).toFixed(2) : "—"; };
  var esc = function (s) { return String(s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };
  var COMP = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  var cardinal = function (d) { return has(d) ? COMP[Math.round((((d % 360) + 360) % 360) / 22.5) % 16] : "—"; };

  // ── messages over the stage ──────────────────────────────────────────
  function message(h, p) { $("msgH").textContent = h; $("msgP").textContent = p; $("msg").hidden = false; }
  function clearMessage() { $("msg").hidden = true; }

  // ── clock + freshness ────────────────────────────────────────────────
  function tick() {
    var n = new Date();
    try {
      $("clock").textContent = n.toLocaleTimeString("en-US", { timeZone: tz, hour: "numeric", minute: "2-digit" });
      $("date").textContent = n.toLocaleDateString("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric" });
    } catch (e) {
      $("clock").textContent = n.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    }
    if (!D || !D.stations.length) return;
    var c = D.stations[sel].current, at = has(c.dateutc) ? c.dateutc : D.generated;
    var age = Math.max(0, Math.round((Date.now() - at) / 1000));
    var txt = age < 90 ? "updated " + age + "s ago" : age < 5400 ? "updated " + Math.round(age / 60) + " min ago" : "updated " + Math.round(age / 3600) + " h ago";
    $("ago").textContent = txt;
    var dot = $("dot");
    dot.classList.toggle("stale", age >= 600 && age < 1800);
    dot.classList.toggle("dead", age >= 1800);
    var night = D.sun && (Date.now() > D.sun[1] || Date.now() < D.sun[0]);
    $("stage").classList.toggle("night", !!night && !params.has("nodim"));
  }
  setInterval(tick, 1000);

  // ── compass ticks ────────────────────────────────────────────────────
  (function () {
    var g = "";
    for (var i = 0; i < 72; i++) {
      var a = i * 5 * Math.PI / 180, long = i % 18 === 0, mid = i % 6 === 0, r1 = 104, r2 = long ? 90 : mid ? 95 : 99;
      g += '<line x1="' + (120 + r1 * Math.sin(a)) + '" y1="' + (120 - r1 * Math.cos(a)) + '" x2="' + (120 + r2 * Math.sin(a)) + '" y2="' + (120 - r2 * Math.cos(a)) + '" stroke="' + (long ? "#8094A4" : "#2C3E4D") + '" stroke-width="' + (long ? 2 : 1) + '"/>';
    }
    $("ticks").innerHTML = g;
  })();

  function setText(id, text) {
    var el = $(id);
    if (el.textContent !== text) {
      var changed = el.textContent !== "" && el.textContent !== "—";
      el.textContent = text;
      if (changed) { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
    }
  }

  function rainingNow(st) {
    var c = st.current, rh = st.rain_hours || [];
    var last = rh.length ? rh[rh.length - 1] : null;
    return (has(c.hourlyrainin) && c.hourlyrainin > 0.01) || (has(last) && last > 0.01);
  }

  function condition(st) {
    var c = st.current;
    if (has(c.hourlyrainin) && c.hourlyrainin >= 0.3) return "Heavy rain";
    if (rainingNow(st)) return "Rain";
    var today = (D.forecast || [])[0] || {}, code = has(today.now_code) ? today.now_code : today.code;
    if (has(code)) {
      if (code >= 95) return "Thunderstorms nearby";
      if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return "Showers likely";
      if (code >= 71 && code <= 77) return "Snow likely";
      if (code === 45 || code === 48) return "Fog";
      if (code === 3) return "Overcast";
      if (code === 2) return "Partly cloudy";
    }
    if (has(c.solarradiation) && c.solarradiation > 500) return "Sunny";
    var night = D.sun && (Date.now() > D.sun[1] || Date.now() < D.sun[0]);
    return night ? "Clear night" : "Clear";
  }

  function aqi(pm) {
    if (!has(pm)) return null;
    if (pm <= 9) return ["Good", "#4ED48A"]; if (pm <= 35.4) return ["Moderate", "#FFD64D"];
    if (pm <= 55.4) return ["Sensitive", "#FFB547"]; return ["Unhealthy", "#FF5A5F"];
  }
  function uvCat(u) {
    if (!has(u)) return null;
    if (u < 3) return ["Low", "#4ED48A"]; if (u < 6) return ["Moderate", "#FFD64D"];
    if (u < 8) return ["High", "#FFB547"]; if (u < 11) return ["Very high", "#FF5A5F"]; return ["Extreme", "#FF80CC"];
  }

  function tile(lbl, val, unit, sub, color, pill) {
    return '<div class="panel tile"><span class="lbl">' + lbl + (pill ? '<span class="pill" style="background:' + pill[1] + '">' + esc(pill[0]) + "</span>" : "") +
      '</span><span class="v" style="color:' + color + '">' + val + "<small>" + unit + '</small></span><span class="sub">' + sub + "</span></div>";
  }

  // Absent is not zero: a tile appears only for a reading the station has.
  function tiles(st) {
    var c = st.current, out = [];
    if (has(c.humidity)) out.push(tile("Humidity", r0(c.humidity), "%", has(c.humidityin) ? "Indoor " + r0(c.humidityin) + "%" : "Outdoor", "var(--hum)"));
    if (has(c.dewPoint)) out.push(tile("Dew point", r0(c.dewPoint), "°F", c.dewPoint >= 65 ? "Oppressive" : c.dewPoint >= 55 ? "Muggy" : c.dewPoint >= 45 ? "Comfortable" : "Dry", "var(--hum)"));
    if (has(c.baromrelin)) {
      var tr = st.press3h, arrow = !has(tr) ? "3 h trend building" : tr > 0.02 ? "▲ rising " : tr < -0.02 ? "▼ falling " : "▶ steady ";
      out.push(tile("Pressure", f2(c.baromrelin), "inHg", arrow + (has(tr) ? (tr > 0 ? "+" : "") + f2(tr) + " in 3 h" : ""), "var(--press)"));
    }
    if (has(c.dailyrainin)) out.push(tile("Rain today", f2(c.dailyrainin), "in", has(c.hourlyrainin) ? "Last hour " + f2(c.hourlyrainin) + " in" : "Day total", "var(--rain)"));
    if (has(c.uv)) out.push(tile("UV index", f1(c.uv), "", (uvCat(c.uv) || [""])[0], "var(--uv)", uvCat(c.uv)));
    if (has(c.solarradiation)) out.push(tile("Solar", r0(c.solarradiation), "W/m²", c.solarradiation > 0 ? "Daylight" : "Sun is down", "var(--solar)"));
    if (D.air && (has(D.air.pm25) || has(D.air.co2))) {
      if (has(D.air.pm25)) out.push(tile("PM2.5", f1(D.air.pm25), "µg/m³", has(D.air.co2) ? "CO₂ " + r0(D.air.co2) + " ppm" : esc(D.air.name), aqi(D.air.pm25)[1], aqi(D.air.pm25)));
      else out.push(tile("CO₂", r0(D.air.co2), "ppm", esc(D.air.name), "#4ED48A"));
    }
    var lt = null;
    for (var i = 0; i < D.stations.length; i++) {
      var x = D.stations[i].current;
      if (has(x.lightning_distance_mi) || has(x.lightning_last_3hr) || has(x.lightning_last_1hr)) { lt = D.stations[i]; break; }
    }
    if (lt) {
      var lc = lt.current, n3 = has(lc.lightning_last_3hr) ? lc.lightning_last_3hr : lc.lightning_last_1hr;
      out.push(tile("Lightning", has(n3) ? r0(n3) : "0", "strikes", has(lc.lightning_distance_mi) ? "Nearest " + f1(lc.lightning_distance_mi) + " mi away" : "Past 3 hours", "var(--bolt)"));
    }
    // Fill to eight with readings that are still worth a glance.
    var extras = [];
    if (has(c.feelsLike)) {
      // A short line that fits the tile, named by the same air-temperature
      // gates the server uses: a heat index from 80°F (it can read BELOW
      // the air in dry heat), a wind chill at 50°F and under. No air
      // reading, no claim (CodeRabbit, PR #145).
      var fsub = "";
      if (has(c.tempf)) {
        var fd = c.feelsLike - c.tempf;
        if (Math.abs(fd) < 1) fsub = "Same as the air";
        else if (c.tempf >= 80) fsub = "Heat index";
        else if (c.tempf <= 50) fsub = "Wind chill";
      }
      extras.push(tile("Feels like", r0(c.feelsLike), "°F", fsub, "var(--feels)"));
    }
    if (has(c.tempinf)) extras.push(tile("Indoor", r0(c.tempinf), "°F", has(c.humidityin) ? r0(c.humidityin) + "% humidity" : "Inside", "var(--temp)"));
    if (has(st.gust24)) extras.push(tile("Peak gust", r0(st.gust24), "mph", "Past 24 hours", "var(--wind)"));
    if (D.sun) { var mins = Math.round((D.sun[1] - D.sun[0]) / 60000); extras.push(tile("Daylight", Math.floor(mins / 60) + "h " + (mins % 60), "m", "Sunrise to sunset", "var(--solar)")); }
    while (out.length < 8 && extras.length) out.push(extras.shift());
    return out.slice(0, 8).join("");
  }

  // ── charts ───────────────────────────────────────────────────────────
  function spark(t, vals, o) {
    var W = 280, H = 118, P = 4, pts = [];
    for (var i = 0; i < t.length; i++) if (has(vals[i])) pts.push([t[i], vals[i]]);
    if (pts.length < 2) return '<div class="none">No readings in the past day</div>';
    var all = pts.map(function (p) { return p[1]; });
    if (o.overlay) o.overlay.forEach(function (v) { if (has(v)) all.push(v); });
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
    if (o.bars) lo = 0;
    if (hi - lo < 1e-4) hi = lo + (o.bars ? 0.1 : 1);
    var pad = (hi - lo) * 0.12, y0 = o.bars ? 0 : lo - pad, y1 = hi + pad;
    var tmin = t[0], tmax = t[t.length - 1];
    var X = function (x) { return P + (x - tmin) / Math.max(1, tmax - tmin) * (W - 2 * P); };
    var Y = function (v) { return H - P - (v - y0) / (y1 - y0) * (H - 2 * P); };
    var s = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" aria-hidden="true">';
    [1 / 3, 2 / 3].forEach(function (f) { var yy = P + f * (H - 2 * P); s += '<line x1="0" x2="' + W + '" y1="' + yy + '" y2="' + yy + '" stroke="#22313E" stroke-width="1"/>'; });
    if (o.bars) {
      var bw = (W - 2 * P) / Math.max(1, t.length);
      pts.forEach(function (p) { if (p[1] > 0) s += '<rect x="' + (X(p[0]) - bw / 2 + 1) + '" y="' + Y(p[1]) + '" width="' + Math.max(1, bw - 2) + '" height="' + (H - P - Y(p[1])) + '" rx="1.5" fill="' + o.color + '"/>'; });
    } else {
      var segs = [], cur = [];
      for (var k = 0; k < t.length; k++) { if (has(vals[k])) cur.push([t[k], vals[k]]); else if (cur.length) { segs.push(cur); cur = []; } }
      if (cur.length) segs.push(cur);
      segs.forEach(function (sg) {
        var d = sg.map(function (p, i) { return (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1); }).join(" ");
        if (sg.length > 1) s += '<path d="' + d + " L" + X(sg[sg.length - 1][0]) + " " + (H - P) + " L" + X(sg[0][0]) + " " + (H - P) + ' Z" fill="' + o.color + '" fill-opacity=".14"/>';
        s += '<path d="' + d + '" fill="none" stroke="' + o.color + '" stroke-width="2"/>';
      });
      if (o.overlay) {
        var od = "", started = false;
        for (var j = 0; j < t.length; j++) { if (has(o.overlay[j])) { od += (started ? "L" : "M") + X(t[j]).toFixed(1) + " " + Y(o.overlay[j]).toFixed(1) + " "; started = true; } else started = false; }
        if (od) s += '<path d="' + od + '" fill="none" stroke="' + o.ocolor + '" stroke-width="1.4" stroke-opacity=".9" stroke-dasharray="3 3"/>';
      }
      var last = pts[pts.length - 1];
      s += '<circle cx="' + X(last[0]) + '" cy="' + Y(last[1]) + '" r="3.5" fill="' + o.color + '" stroke="#0B1218" stroke-width="1.5"/>';
    }
    s += '<text x="6" y="14" font-family="JetBrains Mono,monospace" font-size="10" fill="#8094A4">' + (+hi).toFixed(o.fixed) + "</text>";
    if (!o.bars) s += '<text x="6" y="' + (H - 8) + '" font-family="JetBrains Mono,monospace" font-size="10" fill="#8094A4">' + (+lo).toFixed(o.fixed) + "</text>";
    return s + "</svg>";
  }

  function charts(st) {
    var c = st.current, s = st.series, t = st.t;
    var rh = st.rain_hours || [], ht = [];
    var h0 = t.length ? Math.floor((t[0] - 300000) / 3600000) * 3600000 : 0;
    for (var i = 0; i < rh.length; i++) ht.push(h0 + i * 3600000 + 1800000);
    var anyRain = rh.some(has);
    var list = [
      ["Temperature", has(c.tempf) ? r0(c.tempf) + "°" : "—", spark(t, s.tempf, { color: "#FF9E33", overlay: s.feelsLike, ocolor: "#FF6B5F", fixed: 0 }), "var(--temp)"],
      ["Wind & gust", has(c.windspeedmph) ? f1(c.windspeedmph) + " mph" : "—", spark(t, s.windspeedmph, { color: "#39C9D6", overlay: s.windgustmph, ocolor: "#9BE7EE", fixed: 0 }), "var(--wind)"],
      ["Pressure", f2(c.baromrelin), spark(t, s.baromrelin, { color: "#B39DFF", fixed: 2 }), "var(--press)"],
      ["Rain by hour", has(c.dailyrainin) ? f2(c.dailyrainin) + " in" : "—", anyRain ? spark(ht, rh, { color: "#5AA0FF", bars: true, fixed: 2 }) : '<div class="none">No rain gauge</div>', "var(--rain)"]
    ];
    $("charts").innerHTML = list.map(function (x) {
      return '<div class="panel chart"><div class="head"><span class="lbl">' + x[0] + ' · 24 h</span><span class="now" style="color:' + x[3] + '">' + x[1] + "</span></div>" + x[2] + '<div class="axis"><span>24h ago</span><span>12h</span><span>now</span></div></div>';
    }).join("");
  }

  // ── forecast strip ───────────────────────────────────────────────────
  function icon(code) {
    var c = "#C7D3DC", y = "#FFD64D", b = "#5AA0FF", g;
    var cloud = '<path d="M8 21h14a5 5 0 0 0 0-10 7 7 0 0 0-13 2 4 4 0 0 0-1 8z" fill="none" stroke="' + c + '" stroke-width="1.8" stroke-linejoin="round"/>';
    if (!has(code)) g = cloud;
    else if (code === 0) { g = '<circle cx="15" cy="15" r="5.5" fill="none" stroke="' + y + '" stroke-width="1.8"/>'; [0, 45, 90, 135, 180, 225, 270, 315].forEach(function (a) { var r = a * Math.PI / 180; g += '<line x1="' + (15 + 9 * Math.cos(r)) + '" y1="' + (15 + 9 * Math.sin(r)) + '" x2="' + (15 + 12 * Math.cos(r)) + '" y2="' + (15 + 12 * Math.sin(r)) + '" stroke="' + y + '" stroke-width="1.8" stroke-linecap="round"/>'; }); }
    else if (code <= 2) g = '<circle cx="11" cy="10" r="4.5" fill="none" stroke="' + y + '" stroke-width="1.8"/>' + cloud;
    else if (code === 45 || code === 48) { g = ""; [9, 14, 19, 24].forEach(function (yy, i) { g += '<line x1="' + (4 + i % 2 * 3) + '" y1="' + yy + '" x2="' + (26 - i % 2 * 3) + '" y2="' + yy + '" stroke="' + c + '" stroke-width="1.8" stroke-linecap="round"/>'; }); }
    else if (code >= 95) g = cloud + '<path d="M15 21l-3 5h4l-2 4" fill="none" stroke="' + y + '" stroke-width="1.8" stroke-linejoin="round"/>';
    else if (code >= 71 && code <= 77) { g = cloud; [10, 15, 20].forEach(function (x) { g += '<circle cx="' + x + '" cy="26" r="1.4" fill="' + c + '"/>'; }); }
    else if (code >= 51) { g = cloud; [10, 15, 20].forEach(function (x) { g += '<line x1="' + x + '" y1="24" x2="' + (x - 2) + '" y2="28" stroke="' + b + '" stroke-width="1.8" stroke-linecap="round"/>'; }); }
    else g = cloud;
    return '<svg class="ic" viewBox="0 0 30 30" aria-hidden="true">' + g + "</svg>";
  }

  function forecast() {
    var f = (D.forecast || []).filter(function (d) { return has(d.hi) && has(d.lo); }).slice(0, 7);
    if (!f.length) { $("fc").innerHTML = '<div class="none">Forecast unavailable right now</div>'; return; }
    var lo = Math.min.apply(null, f.map(function (d) { return d.lo; })), hi = Math.max.apply(null, f.map(function (d) { return d.hi; })), span = Math.max(1, hi - lo);
    $("fc").innerHTML = f.map(function (d, i) {
      var name = i === 0 ? "Today" : new Date(d.day + "T12:00:00Z").toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
      var l = (d.lo - lo) / span * 100, w = Math.max(6, (d.hi - d.lo) / span * 100);
      return '<div class="fd' + (i === 0 ? " today" : "") + '"><span class="d">' + name + "</span>" + icon(d.code) +
        '<div class="rng"><span class="lo">' + r0(d.lo) + '°</span><span class="track"><i style="left:' + l + "%;width:" + w + '%"></i></span><span class="hi">' + r0(d.hi) + "°</span></div>" +
        '<span class="pop">' + (has(d.pop) && d.pop > 0 ? d.pop + "% rain" : "dry") + "</span></div>";
    }).join("");
  }

  // 2.8 sunset outlook: "90% · looks good" beside the sunset time, the full
  // line as the tooltip. Hidden when the server has no score (absent is not 0).
  function sunsetChip(s) {
    var el = $("sunChip");
    if (!s || typeof s.pct !== "number") { el.hidden = true; return; }
    el.textContent = s.pct + "% · " + (s.verdict || "");
    el.title = s.line || "";
    el.classList.toggle("good", s.pct >= 70);
    el.hidden = false;
  }

  function banner() {
    var a = (D.alerts || [])[0];
    if (!a) { $("banner").hidden = true; return; }
    $("banner").hidden = false;
    $("banner").classList.toggle("watch", !a.warning);
    $("bTag").textContent = a.warning ? "Warning" : "Alert";
    $("bTxt").textContent = a.headline || (a.event + (has(a.ends_ms) ? " until " + fmtTime(a.ends_ms) : ""));
    $("bMore").textContent = D.alerts.length > 1 ? "+" + (D.alerts.length - 1) + " more" : "";
  }

  function render() {
    var st = D.stations[sel], c = st.current;
    $("place").textContent = D.place || "Weather";
    $("stName").textContent = st.name;
    var chips = $("chips").children;
    for (var i = 0; i < chips.length; i++) chips[i].setAttribute("aria-pressed", String(i === sel));
    setText("tNow", r0(c.tempf));
    $("cond").textContent = condition(st);
    $("feelRow").hidden = !has(c.feelsLike) && !has(c.dewPoint);
    $("feels").textContent = has(c.feelsLike) ? r0(c.feelsLike) + "°" : "—";
    $("dewHero").textContent = has(c.dewPoint) ? r0(c.dewPoint) + "°" : "—";
    setText("hi", st.hi ? r0(st.hi[1]) + "°" : "—"); $("hiT").textContent = st.hi ? fmtTime(st.hi[0]) : "";
    setText("lo", st.lo ? r0(st.lo[1]) + "°" : "—"); $("loT").textContent = st.lo ? fmtTime(st.lo[0]) : "";
    setText("g24", r0(st.gust24));
    if (has(c.winddir)) $("needle").style.transform = "rotate(" + c.winddir + "deg)";
    $("wDir").textContent = cardinal(c.winddir);
    $("wDeg").textContent = has(c.winddir) ? Math.round(c.winddir) + "°" : "";
    setText("wSpd", f1(c.windspeedmph)); setText("wGst", f1(c.windgustmph));
    $("tiles").innerHTML = tiles(st);
    charts(st);
    var storming = rainingNow(st) && !params.has("nostorm");
    $("stage").classList.toggle("storming", storming);
    if (storming) {
      var rh = (st.rain_hours || []).filter(has), peak = rh.length ? Math.max.apply(null, rh) : null;
      $("sTot").textContent = f2(c.dailyrainin);
      $("sRate").textContent = has(c.hourlyrainin) ? f2(c.hourlyrainin) : f2(rh[rh.length - 1]);
      $("sPeak").textContent = f2(peak);
      $("sTemp").textContent = has(c.tempf) ? r0(c.tempf) + "°" : "—";
      $("sNote").textContent = st.name + " · shown while rain is falling";
    }
    tick();
  }

  function buildChips() {
    $("chips").innerHTML = D.stations.map(function (s, i) {
      return '<button class="chip" type="button" data-i="' + i + '" aria-pressed="' + (i === sel) + '">' + esc(s.name) + "</button>";
    }).join("");
    $("chips").hidden = D.stations.length < 2;
  }
  $("chips").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-i]");
    if (!b) return;
    sel = +b.getAttribute("data-i"); render(); restartRotate();
  });

  function restartRotate() {
    if (rotateTimer) clearInterval(rotateTimer);
    if (rotateS && D && D.stations.length > 1) rotateTimer = setInterval(function () { sel = (sel + 1) % D.stations.length; render(); }, rotateS * 1000);
  }

  // ── data ─────────────────────────────────────────────────────────────
  function load() {
    var url = token ? "/api/display" : "/display/data";
    var opts = { cache: "no-store", headers: {} };
    if (token) opts.headers.Authorization = "Bearer " + token;
    fetch(url, opts).then(function (r) {
      if (r.status === 401 || r.status === 403) {
        forgetToken(); token = ""; stopRefresh();
        message("This display link no longer works", "It was revoked or mistyped. Open Settings in the Zasder Weather app, choose Station display link, and open the new link on this screen.");
        return null;
      }
      if (r.status === 404 && !token) {
        message("Nothing to show yet", "This server's public pages are off. Open Settings in the Zasder Weather app, choose Station display link, and open that link on this screen.");
        return null;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (data) {
      if (!data) return;
      if (data.can_write) {
        // An owner key on a wall display is a key anyone in the room can
        // read. Refuse it and forget it; a view-only link does the job.
        forgetToken(); token = ""; stopRefresh();
        message("Use a view-only link", "This link carries full owner access. In the Zasder Weather app, open Settings and choose Station display link to make a view-only one for this screen.");
        return;
      }
      if (!data.stations || !data.stations.length) { message("No stations yet", "Readings appear here as soon as a station reports."); return; }
      var first = !D;
      tz = data.tz || undefined;
      try { new Date().toLocaleTimeString("en-US", { timeZone: tz }); } catch (e) { tz = undefined; }
      var prevId = D && D.stations[sel] ? D.stations[sel].id : startStation;
      D = data;
      var idx = prevId ? D.stations.findIndex(function (s) { return s.id === prevId; }) : -1;
      sel = idx >= 0 ? idx : Math.min(sel, D.stations.length - 1);
      if (first || D.stations.length !== $("chips").children.length) buildChips();
      if (D.sun) { $("sunRow").hidden = false; $("sr").textContent = fmtTime(D.sun[0]); $("ss").textContent = fmtTime(D.sun[1]); }
      sunsetChip(D.sunset_outlook);
      forecast(); banner(); render(); clearMessage();
      if (first) restartRotate();
    }).catch(function () {
      if (!D) message("Can't reach the weather server", "Retrying every 30 seconds. Check this screen's network connection.");
      else { $("dot").classList.add("dead"); $("ago").textContent = "reconnecting"; }
    });
  }

  if (token && /^zww_/.test(token)) {
    forgetToken(); token = "";
    message("Use a view-only link", "This link can change settings. In the Zasder Weather app, open Settings and choose Station display link to make a view-only one for this screen.");
  } else {
    load();
    refreshTimer = setInterval(load, REFRESH_MS);
  }

  // A real vane sways between readings; keep the needle alive gently.
  if (!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches)) {
    setInterval(function () {
      if (!D) return;
      var c = D.stations[sel].current;
      if (!has(c.winddir) || !has(c.windspeedmph) || c.windspeedmph < 0.5) return;
      var w = (Math.random() - 0.5) * 10 * Math.min(1, c.windspeedmph / 3 + 0.2);
      $("needle").style.transform = "rotate(" + (c.winddir + w) + "deg)";
    }, 2600);
  }
})();
