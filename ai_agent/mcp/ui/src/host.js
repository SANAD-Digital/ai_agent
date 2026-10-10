  /* ---------- host bridge: MCP Apps standard (spec 2026-01-26), JSON-RPC over postMessage ---------- */
  var hostCaps = {}, hostCtx = {}, toolInput = null, hostStyle = null;
  var VIEW_NAME = "ai-agent-view";
  /* Lifecycle telemetry (app-only tool): queued, sent as ONE call per view once rendering settles.
     Best effort: never blocks rendering, never carries data, and is dropped when the host does not proxy tools. */
  var traceQueue = [], traceTimer = null, traceSent = 0;
  function trace(event, detail) {
    if (host.standalone || traceSent >= 3) return;
    traceQueue.push({ event: event, detail: detail ? String(detail).slice(0, 200) : undefined });
    if (event === "ui_render_completed" || event === "ui_render_failed") flushTrace(300);
  }
  function flushTrace(delay) {
    clearTimeout(traceTimer);
    traceTimer = setTimeout(function () {
      if (!traceQueue.length || !hostCaps.serverTools) return;
      var batch = traceQueue.splice(0, 12); traceSent++;
      request("tools/call", { name: "view__event", arguments: { events: batch, view: VIEW_NAME } }).catch(function () {});
    }, delay);
  }

  function post(msg) { try { window.parent.postMessage(msg, "*"); } catch (e) {} }
  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      post({ jsonrpc: "2.0", id: id, method: method, params: params || {} });
      setTimeout(function () { if (pending[id]) { delete pending[id]; reject(new Error(T("The request timed out"))); } }, 60000);
    });
  }
  function notify(method, params) { post({ jsonrpc: "2.0", method: method, params: params || {} }); }

  window.addEventListener("message", function (event) {
    var m = event.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id != null && !m.method && pending[m.id]) {
      var p = pending[m.id]; delete pending[m.id];
      if (m.error) p.reject(new Error(m.error.message || T("Error"))); else p.resolve(m.result);
      return;
    }
    // Requests from the host must always get an answer: ping with a result, anything unknown with "method not found".
    if (m.method && m.id != null && m.method !== "ui/resource-teardown") {
      if (m.method === "ping") post({ jsonrpc: "2.0", id: m.id, result: {} });
      else post({ jsonrpc: "2.0", id: m.id, error: { code: -32601, message: "Method not found: " + m.method } });
      return;
    }
    if (m.method === "ui/notifications/tool-result") { stack = []; trace("ui_tool_result_received", (presentationOf(m.params) || {}).type || "text"); show(m.params); }
    else if (m.method === "ui/notifications/tool-input") { toolInput = m.params && m.params.arguments; if (!current) loading(T("Loading…")); }
    else if (m.method === "ui/notifications/tool-cancelled") { stack = []; show({ isError: true, content: [{ type: "text", text: m.params && m.params.reason ? T("The tool run was cancelled: {0}.", [m.params.reason]) : T("The tool run was cancelled.") }] }); }
    else if (m.method === "ui/notifications/host-context-changed") applyContext(m.params);
    else if (m.method === "ui/resource-teardown" && m.id != null) post({ jsonrpc: "2.0", id: m.id, result: {} });
  });

  /* Host context: theme, locale, styles, dimensions, platform. The UI never assumes a width. */
  function applyContext(ctx) {
    if (!ctx) return;
    Object.assign(hostCtx, ctx);
    var rootEl = document.documentElement;
    if (ctx.theme) rootEl.setAttribute("data-theme", ctx.theme);
    if (ctx.locale) { rootEl.setAttribute("lang", ctx.locale); rootEl.setAttribute("dir", /^(ar|he|fa|ur)/i.test(ctx.locale) ? "rtl" : "ltr"); }
    // The layout is ours; the palette is the host's when it offers one. The standard style variables land on the root
    // element, where the tokens in styles.css pick them up through their var() fallbacks. Only standard names are taken.
    var vars = ctx.styles && ctx.styles.variables;
    if (vars) Object.keys(vars).forEach(function (k) {
      if (/^--(color|font|border|shadow)-[a-z0-9-]+$/.test(k) && typeof vars[k] === "string" && vars[k]) rootEl.style.setProperty(k, vars[k]);
      else if (vars[k] == null) rootEl.style.removeProperty(k);
    });
    if (ctx.platform) rootEl.setAttribute("data-platform", ctx.platform);
    if (ctx.displayMode) rootEl.setAttribute("data-display", ctx.displayMode);
    var d = ctx.containerDimensions;
    if (d) { var mh = d.height || d.maxHeight; if (mh) rootEl.style.setProperty("--max-table-height", Math.max(240, mh - 220) + "px"); else rootEl.style.removeProperty("--max-table-height"); }
    var i = ctx.safeAreaInsets;
    if (i) document.body.style.padding = [i.top, i.right, i.bottom, i.left].map(function (n) { return Math.max(0, n) + "px"; }).join(" ");
    // Theme, direction and size are applied by CSS. The view is rebuilt only when the text direction really changed,
    // so what the user sorted, filtered or opened is kept.
    var dir = rootEl.getAttribute("dir");
    if (current && lastDir && dir !== lastDir) show(current.result, true);
    lastDir = dir;
    setTimeout(reportSize, 0);
  }
  var lastDir = null;

  /* ---------- capability layer ----------
     One rule for every feature: (1) the MCP Apps standard capability the host declared,
     (2) a vendor API only when the standard has no equivalent AND the function exists,
     (3) a plain fallback. The UI itself never branches on which host it runs in. */
  var memoryState = {};
  function sessionOk() { try { return !!window.sessionStorage; } catch (e) { return false; } }
  function vendor() { return window.openai && typeof window.openai === "object" ? window.openai : null; }

  var Host = {
    has: function (name) {
      var v = vendor();
      switch (name) {
        case "openLinks": return !!hostCaps.openLinks || !!(v && typeof v.openExternal === "function");
        case "downloadFile": return !!hostCaps.downloadFile;
        case "serverTools": return !!hostCaps.serverTools || !!(v && typeof v.callTool === "function") || !!window.__mockCall;
        case "updateModelContext": return !!hostCaps.updateModelContext;
        case "message": return !!hostCaps.message || !!(v && typeof v.sendFollowUpMessage === "function");
        case "fullscreen": return (hostCtx.availableDisplayModes || []).indexOf("fullscreen") >= 0;
        case "persistState": return !!(v && typeof v.setWidgetState === "function") || sessionOk();
      }
      return false;
    },
    callTool: function (name, args) {
      if (window.__mockCall) return Promise.resolve(window.__mockCall(name, args));
      if (hostCaps.serverTools) return request("tools/call", { name: name, arguments: args || {} });
      var v = vendor();
      if (v && typeof v.callTool === "function") return Promise.resolve(v.callTool(name, args || {}));
      return Promise.reject(new Error(T("This host app does not allow running tools from the card.")));
    },
    openLink: function (url) {
      if (!url) return;
      if (hostCaps.openLinks) return request("ui/open-link", { url: url }).catch(function () { Host.copy(url); });
      var v = vendor();
      if (v && typeof v.openExternal === "function") { try { v.openExternal({ href: url }); return; } catch (e) {} }
      if (host.standalone) { window.open(url, "_blank"); return; }
      Host.copy(url);
    },
    download: function (name, mime, text) {
      if (hostCaps.downloadFile) {
        return request("ui/download-file", { contents: [{ type: "resource", resource: { uri: "file:///" + name, mimeType: mime, text: text } }] })
          .then(function (r) { if (r && r.isError) Host.copy(text); }).catch(function () { Host.copy(text); });
      }
      Host.copy(text);
    },
    copy: function (text) {
      var done = function () { flash(T("Copied")); };
      function fallback() {
        var ta = h("textarea", { style: "position:fixed;opacity:0" }); ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); done(); } catch (e) { flash(T("Could not copy")); } ta.remove();
      }
      try { navigator.clipboard.writeText(text).then(done, fallback); } catch (e) { fallback(); }
    },
    tellModel: function (text) {
      // Context for the next turn only; never triggers a follow-up by itself.
      if (hostCaps.updateModelContext) request("ui/update-model-context", { content: [{ type: "text", text: text }] }).catch(function () {});
    },
    followUp: function (text) {
      // Asks the assistant something on the user's behalf. Returns false when the host cannot.
      if (hostCaps.message) { request("ui/message", { role: "user", content: [{ type: "text", text: text }] }).catch(function () {}); return true; }
      var v = vendor();
      if (v && typeof v.sendFollowUpMessage === "function") { try { v.sendFollowUpMessage({ prompt: text }); return true; } catch (e) {} }
      return false;
    },
    fullscreen: function (on) {
      if (Host.has("fullscreen")) request("ui/request-display-mode", { mode: on ? "fullscreen" : "inline" }).catch(function () {});
    },
    // View state (density, sort) survives re-renders. The standard has no view-state API:
    // vendor state when present, else sessionStorage, else memory.
    saveState: function (key, value) {
      memoryState[key] = value;
      var v = vendor();
      if (v && typeof v.setWidgetState === "function") { try { v.setWidgetState(Object.assign({}, memoryState)); } catch (e) {} return; }
      if (sessionOk()) { try { sessionStorage.setItem("aiv:" + key, JSON.stringify(value)); } catch (e) {} }
    },
    loadState: function (key) {
      if (key in memoryState) return memoryState[key];
      var v = vendor();
      if (v && v.widgetState && key in v.widgetState) return v.widgetState[key];
      if (sessionOk()) { try { var raw = sessionStorage.getItem("aiv:" + key); if (raw) return JSON.parse(raw); } catch (e) {} }
      return undefined;
    }
  };

  // Thin aliases so components read naturally.
  function callTool(name, args) { return Host.callTool(name, args); }
  function openLink(url) { return Host.openLink(url); }
  function copyText(text) { return Host.copy(text); }
  function downloadCsv(name, csv) { return Host.download(name, "text/csv", csv); }
  function tellModel(text) { return Host.tellModel(text); }
  function loading(text) { clear(root); root.appendChild(h("div", { class: "msg", role: "status", text: text })); root.appendChild(h("div", { class: "skeleton", style: "width:60%" })); }

  var flashTimer;
  function flash(text) {
    var el = document.getElementById("flash");
    if (!el) { el = h("div", { id: "flash", role: "status", style: "position:fixed;bottom:8px;inset-inline-start:12px;background:var(--text);color:var(--surface);border-radius:6px;padding:6px 12px;z-index:50;font-size:13px" }); document.body.appendChild(el); }
    el.textContent = text; el.style.display = "block"; clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { el.style.display = "none"; }, 1600);
  }
  function reportSize() {
    var r = document.documentElement.getBoundingClientRect();
    notify("ui/notifications/size-changed", { width: Math.ceil(r.width), height: Math.ceil(r.height) });
  }
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);

  /* A host that renders through its own bridge instead of the MCP Apps one still hands the result over on its own global object.
     Used only when the standard tool-result has not arrived (nothing is shown yet) and the object really carries a result. */
  function vendorRender() {
    var take = function () {
      var v = vendor();
      if (current || !v || !(v.toolOutput || v.toolResponseMetadata)) return;
      show({ structuredContent: v.toolOutput || {}, _meta: v.toolResponseMetadata || {}, content: [] });
    };
    window.addEventListener("openai:set_globals", take);
    setTimeout(take, 1500);
  }
