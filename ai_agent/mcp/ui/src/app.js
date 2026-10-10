  /* ---------- top level: one entry point, a component per presentation type ---------- */
  var COMPONENTS = {
    table: function (p, opts) { return Table(p, opts); },
    approval: function (p, opts) { return Approval(p, opts); },
    choice: function (p, opts) { return Choice(p, opts); },
    document: function (p, opts) { return Document(p, opts); },
    cards: function (p, opts) { return Cards(p, opts); },
    error: function (p, opts) { return ErrorView(p, opts); }
  };
  function build(result, opts) {
    var backBtn = opts.back ? h("button", { class: "ag-back", type: "button", onclick: back }, icon(isRtl() ? "right" : "left", 16), T("Back")) : null;
    var p = presentationOf(result);
    if (result && result.isError) return h("div", { class: "ag-stack" }, backBtn, Card(ErrorState(textOf(result), result.__retry || null, p && p.type === "error" ? p : null)));
    if (!p) return h("div", null, backBtn, h("pre", { class: "ag-card", style: "margin:0;padding:12px var(--pad);white-space:pre-wrap;font:inherit", text: textOf(result) }));
    var view = (COMPONENTS[p.type] || COMPONENTS.cards)(p, opts);
    return p.type === "table" || p.type === "cards" || p.type === "document" ? view : h("div", null, backBtn, view);
  }
  function show(result, keepStack) {
    current = { result: result };
    var kind = (presentationOf(result) || {}).type || (result && result.isError ? "error" : "text");
    if (needsLoad(result)) return loadFull(result, kind);
    trace("ui_render_started", kind);
    clear(root);
    try {
      root.appendChild(build(result, { back: stack.length > 0 }));
    } catch (e) {
      trace("ui_render_failed", kind + ": " + (e && e.message));
      root.appendChild(Card(ErrorState(T("Could not show the result: {0}", [e && e.message]), null)));
      setTimeout(reportSize, 0);
      return;
    }
    document.title = (presentationOf(result) || {}).title || "AI Agent View";
    trace("ui_render_completed", kind);
    setTimeout(reportSize, 0);
  }
  /* The host gave us only structuredContent: fetch the complete view model, then render it. */
  function loadFull(result, kind) {
    var lean = result.structuredContent.presentation;
    function fallback(reason) {
      // Never an empty table that looks like "no data": show the text the server wrote for exactly this case.
      trace("ui_render_failed", kind + ": " + reason);
      clear(root);
      root.appendChild(Card([h("div", { class: "ag-head" }, h("div", { class: "ag-title" }, h("div", null, h("h1", null, titled(lean.title || ""))))),
        h("pre", { style: "margin:0;padding:0 var(--pad) 14px;white-space:pre-wrap;font:inherit", text: textOf(result) || (result.structuredContent.summary || {}).text || "" })]));
      setTimeout(reportSize, 0);
    }
    if (!Host.has("serverTools")) return fallback("host does not proxy tools and sent no _meta");
    loading(lean.title || T("Loading…"));
    trace("ui_render_started", kind + " (loading full view)");
    callTool("view__load", { ref: lean.ref }).then(function (res) {
      var p = res && res.structuredContent && res.structuredContent.presentation;
      if (!res || res.isError || !p || !p.full) return fallback(friendly({ message: textOf(res) }) || "view__load failed");
      show(res);
    }).catch(function (e) { fallback(friendly(e)); });
  }
  function drill(tool, args) {
    if (!tool) return;
    stack.push(current && current.result);
    clear(root); root.appendChild(h("div", { class: "skeleton", style: "width:50%" })); root.appendChild(h("div", { class: "skeleton", style: "width:85%" }));
    var retry = function () { current = { result: stack.pop() }; drill(tool, args); };   // restore the list we came from, then try again
    callTool(tool, args).then(function (res) {
      if (res && res.isError) res.__retry = retry;   // a failed tool call and a failed request get the same retry
      show(res);
    }).catch(function (e) {
      show({ isError: true, content: [{ type: "text", text: friendly(e) }], __retry: retry });
    });
  }
  function back() { var prev = stack.pop(); if (prev) show(prev); }

  window.__aiAgentView = { show: show };
  /* MCP Apps handshake. The first ui/initialize can be lost when a host starts listening a moment after the document
     runs, so it is repeated until the host answers (a host that already answered just sees one request). */
  function handshake(attempt) {
    var answered = false;
    request("ui/initialize", { protocolVersion: "2026-01-26", appInfo: { name: VIEW_NAME, version: "0.5.0" }, appCapabilities: {} })
      .then(function (res) {
        if (hostReady) return;
        hostReady = answered = true;
        hostCaps = (res && res.hostCapabilities) || {};
        notify("ui/notifications/initialized", {});
        applyContext(res && res.hostContext);
        trace("ui_handshake_completed", ((res && res.hostInfo && res.hostInfo.name) || "?") + (attempt ? " (attempt " + (attempt + 1) + ")" : ""));
        reportSize();
        if (current) flushTrace(300);
      })
      .catch(function () {});
    setTimeout(function () { if (!answered && !hostReady && attempt < 12) handshake(attempt + 1); }, attempt < 4 ? 500 : 1500);
  }
  var hostReady = false;
  if (!host.standalone) { trace("ui_handshake_started"); handshake(0); }
  vendorRender();
