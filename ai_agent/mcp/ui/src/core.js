
  var root = document.getElementById("root");
  var PAGE = 25;
  var host = { standalone: window.parent === window };
  var pending = {}, nextId = 1;
  var stack = [];
  var current = null;

  /* ---------- wording ----------
     English source text, shown in the viewer's language: AI_I18N is filled by the server when it
     builds this document (protocol.build_main_view). Missing entries fall back to English. */
  var I18N = (typeof AI_I18N !== "undefined" && AI_I18N) || {};
  function T(text, args) {
    var s = I18N[text] || text;
    if (args) s = s.replace(/\{(\d+)\}/g, function (m, i) { return args[i] === undefined ? m : args[i]; });
    return s;
  }

  /* ---------- DOM helper ---------- */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === "class") el.className = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else el.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }


  var SVGNS = "http://www.w3.org/2000/svg";
  /* Inline SVG icon from the ICONS map (icons.js). Decorative: hidden from assistive tech. */
  function icon(name, size) {
    var svg = document.createElementNS(SVGNS, "svg");
    var n = size || 18;
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", n); svg.setAttribute("height", n);
    svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round"); svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = (typeof ICONS !== "undefined" && ICONS[name]) || "";
    return svg;
  }
