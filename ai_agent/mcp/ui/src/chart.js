  /* ---------- charts: line, bar, stacked_bar, donut (inline SVG, no dependencies) ----------
     A chart never stands alone: the caller pairs it with "عرض البيانات" for the table behind it. */
  function compact(n) { var a = Math.abs(n), u = [[1e9, "B"], [1e6, "M"], [1e3, "K"]]; for (var i = 0; i < 3; i++) if (a >= u[i][0]) return (n / u[i][0]).toFixed(1).replace(/\.0$/, "") + u[i][1]; return String(Math.round(n * 100) / 100); }
  function ns(tag, attrs) { var e = document.createElementNS(SVGNS, tag); Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); }); return e; }
  var SERIES_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

  function Chart(chart) {
    var box = h("div", { class: "chartbox chartcard" }, h("h2", { text: chart.title || "الرسم" }));
    var series = chart.series || [], labels = chart.labels || [], n = labels.length;
    if (!n || !series.length) return null;
    var avail = root.clientWidth || 640, W = Math.max(300, Math.min(760, Math.floor(avail / (avail >= 820 ? 2 : 1)) - 48)), H = 210, pad = { l: 44, r: 8, t: 14, b: 26 };
    var svg = ns("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": (chart.title || "رسم") + ": " + n + " نقطة" });
    var tip = h("div", { class: "charttip", role: "status", "aria-live": "polite" });
    var plotW = W - pad.l - pad.r, plotH = H - pad.t - pad.b;
    var rtl = document.documentElement.getAttribute("dir") === "rtl";
    var stacked = chart.kind === "stacked_bar";

    var max = 0;
    for (var i = 0; i < n; i++) {
      var idx = i;
      var col = stacked ? series.reduce(function (a, s) { return a + (s.values[idx] || 0); }, 0) : Math.max.apply(null, series.map(function (s) { return s.values[idx] || 0; }));
      if (col > max) max = col;
    }
    if (!max) max = 1;
    // "Nice" axis: three clean steps (1, 2, 5 x 10^k) instead of ticks like 173.33.
    var raw = max / 3, mag = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / mag;
    var step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * mag;
    max = step * 3;

    // Tooltip: category, then the exact value (with currency) of every series so comparisons stay readable.
    function show(i) {
      tip.textContent = "";
      tip.appendChild(h("b", { text: String(labels[i]) }));
      series.forEach(function (s, si) {
        tip.appendChild(h("div", null, series.length > 1 ? h("span", { style: "color:" + SERIES_COLORS[si % SERIES_COLORS.length], text: "■ " }) : null,
          series.length > 1 ? s.name + ": " : "", h("span", { class: "num", text: s.d[i] })));
      });
    }
    function bind(el, i) {
      el.addEventListener("mouseenter", function () { show(i); });
      el.addEventListener("focus", function () { show(i); });
      el.setAttribute("tabindex", "0");
      el.setAttribute("aria-label", labels[i] + ": " + series.map(function (s) { return s.d[i]; }).join(" / "));
    }

    if (chart.kind === "donut") {
      var vals = series[0].values, total = vals.reduce(function (a, b) { return a + b; }, 0) || 1, ang = -Math.PI / 2, cx = W / 2, cy = H / 2, R = 88, r = 54;
      labels.forEach(function (label, i) {
        var frac = vals[i] / total, a2 = ang + frac * Math.PI * 2, large = frac > 0.5 ? 1 : 0;
        var d = "M" + (cx + R * Math.cos(ang)) + " " + (cy + R * Math.sin(ang)) + " A" + R + " " + R + " 0 " + large + " 1 " + (cx + R * Math.cos(a2)) + " " + (cy + R * Math.sin(a2)) +
          " L" + (cx + r * Math.cos(a2)) + " " + (cy + r * Math.sin(a2)) + " A" + r + " " + r + " 0 " + large + " 0 " + (cx + r * Math.cos(ang)) + " " + (cy + r * Math.sin(ang)) + " Z";
        var path = ns("path", { d: d, fill: "var(--series-1)", "fill-opacity": 1 - i * (0.7 / Math.max(1, n)), stroke: "var(--surface)", "stroke-width": 1 });
        bind(path, i); svg.appendChild(path); ang = a2;
      });
      box.appendChild(svg); box.appendChild(tip);
      box.appendChild(h("div", { class: "legend" }, labels.map(function (l, i) { return h("span", { text: "■ " + l + " " + Math.round(vals[i] / total * 100) + "%" }); })));
      return box;
    }

    for (var g = 0; g <= 3; g++) {
      var y = pad.t + plotH * g / 3;
      svg.appendChild(ns("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: "grid" }));
      var yl = ns("text", { x: pad.l - 6, y: y + 4, "text-anchor": "end", class: "ax" }); yl.textContent = compact(max * (1 - g / 3)); svg.appendChild(yl);
    }
    function axis(xfn) {
      var step = Math.max(1, Math.ceil(n / 8));
      labels.forEach(function (l, i) { if (i % step) return; var t = ns("text", { x: xfn(i), y: H - 8, "text-anchor": "middle", class: "ax" }); t.textContent = String(l).slice(0, 12); svg.appendChild(t); });
    }
    if (chart.kind === "line") {
      var px = function (i) { var f = n <= 1 ? 0.5 : i / (n - 1); return pad.l + plotW * (rtl ? 1 - f : f); };
      series.forEach(function (s, si) {
        var pts = s.values.map(function (v, i) { return [px(i), pad.t + plotH * (1 - v / max)]; });
        svg.appendChild(ns("polyline", { points: pts.map(function (q) { return q.join(","); }).join(" "), fill: "none", stroke: SERIES_COLORS[si % SERIES_COLORS.length], "stroke-width": 2, "stroke-linejoin": "round" }));
        pts.forEach(function (q, i) { var c = ns("circle", { cx: q[0], cy: q[1], r: 4, fill: SERIES_COLORS[si % SERIES_COLORS.length], "fill-opacity": n > 24 ? 0 : 1 }); bind(c, i); svg.appendChild(c); });
      });
      axis(px);
    } else {
      var slot = plotW / n, groups = stacked ? 1 : series.length, bw = Math.min(46, slot * (groups > 1 ? 0.8 / groups : 0.6));
      var center = function (i) { return pad.l + slot * (rtl ? n - 1 - i : i) + slot / 2; };
      for (var k = 0; k < n; k++) {
        var acc = 0;
        series.forEach(function (s, si) {
          var v = s.values[k] || 0, bh = plotH * v / max, base = stacked ? acc : 0, x = center(k) - (bw * groups) / 2 + (stacked ? 0 : si * bw);
          var rect = ns("rect", { x: x, y: pad.t + plotH - plotH * base / max - bh, width: Math.max(bw - 2, 1), height: Math.max(bh, 0), rx: stacked ? 0 : 2, fill: SERIES_COLORS[si % SERIES_COLORS.length] });
          bind(rect, k); svg.appendChild(rect); acc += v;
          if (n <= 8 && series.length === 1) {
            var lab = ns("text", { x: x + (bw - 2) / 2, y: Math.max(pad.t + 10, pad.t + plotH - bh - 4), "text-anchor": "middle", class: "ax", style: "font-weight:500" });
            lab.textContent = compact(v); svg.appendChild(lab);
          }
        });
      }
      axis(center);
    }
    box.appendChild(h("div", { class: "legend" }, series.map(function (s, i) { return h("span", null, h("span", { style: "color:" + SERIES_COLORS[i % SERIES_COLORS.length], text: "■ " }), s.name + (series.length === 1 && chart.unit ? " (" + chart.unit + ")" : "")); })));
    box.appendChild(svg); box.appendChild(tip);
    return box;
  }
