  /* ---------- approval card: ready state, blocked state (insufficient stock), edit form, result ---------- */
  function Approval(p, opts) {
    var st = { editing: false, busy: false, error: null, confirmCancel: false, picking: false, chosen: null, details: false };
    var box = h("div", { class: "ag-stack" });
    var byKey = {};
    (p.actions || []).forEach(function (a) { byKey[a.key] = a; });
    function submit(tool, args) {
      st.busy = true; st.error = null; draw();
      callTool(tool, args).then(function (res) {
        var np = presentationOf(res);
        if (res.isError || !np) throw new Error(textOf(res) || T("The request could not be completed"));
        show(res);
        if (np.type === "approval" && ["executed", "verified", "rejected", "failed"].indexOf(np.state) >= 0) tellModel("نتيجة طلب الموافقة " + np.approval_id + ": " + (np.title || "") + " — " + np.state + (np.result && np.result.document_name ? " — " + np.result.document_name : "") + ".");
      }).catch(function (e) { st.busy = false; st.error = friendly(e); draw(); });
    }
    function fact(f) {
      var val = h("div", { class: "val" + (f.link ? " link" : "") }, rich(f.d), f.chip ? h("span", { class: "stockchip" + (f.chip.tone === "danger" ? " bad" : f.chip.tone === "neutral" ? " note" : ""), text: f.chip.d }) : null);
      return h("div", { class: "fact" + (f.wide ? " wide" : "") }, h("div", { class: "k" }, f.icon ? icon(f.icon, 15) : null, f.label), val);
    }
    function photo(src, fallback) {
      var ph = h("div", { class: "photo" });
      if (src) { var img = h("img", { src: src, alt: "" }); img.addEventListener("error", function () { img.remove(); ph.appendChild(icon(fallback || "package", 32)); }); ph.appendChild(img); }
      else ph.appendChild(icon(fallback || "package", 32));
      return ph;
    }
    function footer(buttons) { return h("div", { class: "ag-foot" }, buttons); }
    function cancelBtn() {
      var a = byKey.cancel; if (!a) return null;
      return h("button", { class: "btn danger", type: "button", disabled: st.busy, onclick: function () { st.confirmCancel = true; draw(); } }, icon("x", 16), T("Reject"));
    }
    function editBtn(label) { return byKey.edit ? h("button", { class: "btn soft", type: "button", disabled: st.busy, onclick: function () { st.editing = true; st.error = null; draw(); } }, icon("edit", 16), label || byKey.edit.label) : null; }

    var STATE_LABEL = { pending: "Needs approval", approved: "Approved", executing: "Executing", executed: "Executed", verified: "Executed and verified", rejected: "Rejected", expired: "Expired", failed: "Execution failed" };
    var STATE_TONE = { pending: "warning", approved: "info", executing: "info", executed: "success", verified: "success", rejected: "neutral", expired: "neutral", failed: "danger" };
    function head() {
      var blocked = p.blocked && p.state === "pending";
      var state = st.busy ? "executing" : p.state;
      if (blocked) return h("div", { class: "ag-head" }, h("div", { class: "ag-title", style: "color:var(--danger)" }, icon("alert", 20),
        h("div", null, h("h1", { text: p.blocked.title }), h("div", { class: "ag-sub", text: p.blocked.message }))));
      return h("div", { class: "ag-head" }, h("div", { class: "ag-title" }, h("div", null, h("h1", null, titled(p.title)), p.subtitle ? h("div", { class: "ag-sub", text: p.subtitle }) : null)),
        h("span", { class: "badge lg " + (STATE_TONE[state] || "neutral") + (p.state === "verified" || p.state === "executed" ? " success-mark" : ""), text: (p.state === "pending" && p.status_label) || (STATE_LABEL[state] ? T(STATE_LABEL[state]) : state) }));
    }
    /* Prepare -> Validate -> Review -> Approve -> Execute -> Verify: always visible, so the user knows where the request stands. */
    function steps() {
      var list = (p.steps || []).map(function (s) { return Object.assign({}, s); });
      if (st.busy) list.forEach(function (s) { if (s.key === "approval") s.state = "done"; if (s.key === "execute") s.state = "active"; });
      return h("ol", { class: "steps", "aria-label": T("Request stages") }, list.map(function (s, i) {
        return h("li", { class: "step " + s.state, "aria-current": s.state === "active" ? "step" : null }, h("span", { class: "dot" }, s.state === "done" ? icon("check", 12) : s.state === "error" ? icon("x", 12) : String(i + 1)), s.label);
      }));
    }
    function Impact() {
      var blocks = [];
      if (p.impact_financial && p.impact_financial.length) blocks.push(Section(T("Financial impact"), "coins", FieldGrid(p.impact_financial)));
      if (p.impact_stock && p.impact_stock.length) blocks.push(Section(T("Stock impact"), "boxes", FieldGrid(p.impact_stock)));
      if (p.next_steps && p.next_steps.length) blocks.push(Section(T("What happens after approval"), "next", h("ol", { class: "nextsteps" }, p.next_steps.map(function (t) { return h("li", null, rich(t)); }))));
      return blocks.length ? h("div", { class: "ag-blocks" }, blocks) : null;
    }
    function Details() {
      if (!st.details || !p.details || !p.details.length) return null;
      return h("div", { class: "ag-blocks details" }, p.details.map(function (d) {
        if (d.fields) return Section(d.title, null, FieldGrid(d.fields));
        return Section(d.title, "layers", h("div", { class: "tablewrap" }, h("table", { class: "compact" },
          h("thead", null, h("tr", null, d.columns.map(function (c) { return h("th", { scope: "col", text: c }); }))),
          h("tbody", null, d.rows.map(function (r) { return h("tr", null, r.map(function (c) { return h("td", null, rich(c)); })); })))));
      }));
    }
    function detailsBtn() {
      if (!p.details || !p.details.length) return null;
      return h("button", { class: "btn ghost", type: "button", "aria-expanded": String(!!st.details), onclick: function () { st.details = !st.details; draw(); } }, icon(st.details ? "down" : isRtl() ? "left" : "right", 16), st.details ? T("Hide details") : T("Show details"));
    }
    /* Rejecting is confirmed in place, with what it means, before anything is sent. */
    function ConfirmReject() {
      var a = byKey.cancel;
      return h("div", { class: "confirm", role: "alertdialog", "aria-label": T("Confirm rejection") },
        h("b", { text: T("Reject this request?") }), h("div", { class: "ag-sub", text: T("Nothing will be executed, and you can request the operation again later.") }),
        h("div", { class: "ag-actions" },
          h("button", { class: "btn danger solid", type: "button", disabled: st.busy, onclick: function () { submit(a.tool, a.arguments); } }, icon("x", 16), T("Confirm rejection")),
          h("button", { class: "btn", type: "button", onclick: function () { st.confirmCancel = false; draw(); }, text: T("Don't reject") })));
    }
    function Outcome() {
      if (p.state === "pending" || st.busy) return null;
      var ok = p.state === "executed" || p.state === "verified";
      var text = { verified: "The operation was executed and the document was verified.", executed: "The operation was executed.", rejected: "The request was rejected and nothing was executed.",
                   expired: "The request expired before a decision was made. Request the operation again.",
                   failed: "The operation did not complete and nothing was saved.", approved: "Approved; execution is in progress." }[p.state];
      text = text ? T(text) : "";
      var cz = p.customization;
      if (cz && cz.result && cz.result.text && ok) text = cz.result.text;
      var docs = cz ? [] : p.result && p.result.documents ? p.result.documents : (p.result && p.result.document_name ? [{ doctype: p.result.doctype, document_name: p.result.document_name }] : []);
      return h("div", { class: "outcome " + (ok ? "ok" : p.state === "failed" ? "bad" : "off"), role: "status" },
        h("div", { class: "outcome-head" }, icon(ok ? "checkCircle" : p.state === "failed" ? "alert" : "info", 18), h("b", { text: text })),
        docs.length ? h("div", { class: "finding-docs" }, docs.map(function (d) {
          return h("button", { class: "chip", type: "button", onclick: function () { drill("data__get_document", { doctype: d.doctype, name: d.document_name }); } }, rich(String(d.document_name)));
        })) : null);
    }
    function BlockedBody() {
      var b = p.blocked, unit = b.unit || "";
      var body = [h("div", { class: "blocked" },
        h("div", { class: "blocked-info" },
          h("div", null, h("div", { style: "font-weight:600", text: b.item || "" }), h("span", { class: "stockchip bad", text: T("Insufficient stock") })),
          h("div", { class: "avail" }, h("div", { class: "ag-sub" }, rich(T("Available in the warehouse — requested {0} {1}", [fmtN(b.requested), unit]))), h("div", { class: "big" }, rich(fmtN(b.available) + " " + unit)))),
        photo(b.image))];
      if (st.picking) {
        body.push(h("div", { class: "section-title", text: T("Choose a warehouse that has the item") }));
        body.push(h("div", { class: "altlist", role: "radiogroup", "aria-label": T("Alternative warehouses") }, b.alternatives.length ? b.alternatives.map(function (alt) {
          return h("button", { class: "opt", type: "button", role: "radio", "aria-checked": String(st.chosen === alt.warehouse), onclick: function () { st.chosen = alt.warehouse; draw(); } },
            h("span", { class: "radio" }), icon("warehouse", 18), h("span", { class: "grow", text: alt.warehouse }), h("span", { class: "meta", text: alt.d }));
        }) : h("div", { class: "msg", text: T("No other warehouse has the requested quantity.") })));
        body.push(h("div", { class: "ag-foot" },
          h("button", { class: "btn", type: "button", onclick: function () { st.picking = false; draw(); }, text: T("Back") }),
          h("button", { class: "btn primary", type: "button", disabled: !st.chosen || st.busy, onclick: function () { reviseWith({ fields: { set_warehouse: st.chosen }, items: {} }); } }, icon("check", 16), T("Use this warehouse"))));
      }
      return body;
    }
    function reviseWith(changes) {
      var a = byKey.edit; if (!a) return;
      submit("approvals__revise", { approval_id: a.arguments.approval_id, token: a.arguments.token, changes: changes });
    }
    function DocumentsBody() {
      return [h("div", { class: "docs" }, p.documents.map(function (d) {
        return h("div", { class: "docblock" },
          h("div", { class: "dochead" }, icon(d.icon || "file", 18), h("span", { class: "k", text: d.type_label }), h("b", { text: d.title })),
          d.fields.length ? h("div", { class: "facts" }, d.fields.map(function (f) { return fact({ label: f.label, d: f.d }); })) : null);
      }))];
    }
    function ReadyBody() {
      if (p.documents && p.documents.length) return DocumentsBody();
      var facts = (p.fields || []).map(fact);
      var one = (p.items || []).length <= 1;
      var out = [h("div", { class: "product" + (p.photo ? "" : " nophoto") }, h("div", { class: "facts" }, facts), p.photo ? photo(p.photo) : null)];
      if (!one) out.push(h("div", { class: "section-title", text: T("Line Items") }), h("div", { class: "tablewrap" }, h("table", { class: "compact" },
        h("thead", null, h("tr", null, ["Item", "Quantity", "Rate", "Amount"].map(function (t, i) { return h("th", { scope: "col", class: i ? "end" : "", text: T(t) }); }))),
        h("tbody", null, p.items.map(function (it) {
          return h("tr", null, h("td", { class: "primary", text: it.item || "—" }), ["qty", "rate", "amount"].map(function (k) { return h("td", { class: "end t-currency" }, h("span", { class: "num", text: it.d[k] })); }));
        })))));
      return out;
    }
    function EditForm() {
      var ed = p.editable, a = byKey.edit || {}, args = a.arguments || {};
      var inputs = { fields: {}, items: {} }, form = h("div");
      function input(f, holder, key) {
        var inp = h("input", { type: f.type === "date" ? "date" : f.type === "number" ? "number" : "text", step: "any" });
        inp.value = f.value == null ? "" : f.value; holder[key] = { el: inp, orig: f.value };
        return h("label", { class: "fld" }, f.label, inp);
      }
      form.appendChild(h("div", { class: "form" }, (ed.fields || []).map(function (f) { return input(f, inputs.fields, f.key); })));
      (ed.items || []).forEach(function (it) {
        inputs.items[it.idx] = {};
        form.appendChild(h("div", { class: "section-title", text: it.label || T("Line {0}", [it.idx]) }));
        form.appendChild(h("div", { class: "form" }, it.fields.map(function (f) { return input(f, inputs.items[it.idx], f.key); })));
      });
      function differs(o) { return String(o.el.value) !== String(o.orig == null ? "" : o.orig); }
      form.appendChild(footer([
        h("button", { class: "btn", type: "button", onclick: function () { st.editing = false; draw(); }, text: T("Back") }),
        h("button", { class: "btn primary grow", type: "button", disabled: st.busy, onclick: function () {
          var changes = { fields: {}, items: {} };
          Object.keys(inputs.fields).forEach(function (k) { if (differs(inputs.fields[k])) changes.fields[k] = inputs.fields[k].el.value; });
          Object.keys(inputs.items).forEach(function (i) { Object.keys(inputs.items[i]).forEach(function (k) { if (differs(inputs.items[i][k])) { changes.items[i] = changes.items[i] || {}; changes.items[i][k] = inputs.items[i][k].el.value; } }); });
          submit("approvals__revise", { approval_id: args.approval_id, token: args.token, changes: changes });
        } }, icon("check", 16), st.busy ? T("Verifying…") : T("Save and re-check"))]));
      return form;
    }
    /* ---------- customization request: what changes, the full code, the findings, who it affects, how to undo ---------- */
    function czSection(glyph, title, content, extra) {
      return h("section", { class: "cz-sec" }, h("h2", { class: "ag-block-title" }, icon(glyph, 16), title, extra || null), content);
    }
    function CustomizationBody(c) {
      var L = c.labels || {}, code = null, marks = {};
      (c.checks || []).forEach(function (k) { if (k.line && (!marks[k.line] || k.level === "critical")) marks[k.line] = k.level; });
      if (c.code) code = CodeBlock(c.code, marks, L);
      function goTo(n) { if (code) code.goTo(n); }
      var out = [];
      var who = c.provenance && c.provenance.requested_by;
      out.push(h("div", { class: "cz-meta" },
        h("span", { class: "badge " + (c.risk_tone || "neutral"), text: c.risk_label }),
        h("span", { class: "cz-chip", text: [c.kind_label, c.operation_label].filter(Boolean).join(" · ") }),
        who ? h("span", { class: "cz-who" }, icon("user", 14), h("span", { class: "ltr", text: who + (c.provenance.surface ? " · " + c.provenance.surface : "") })) : null));
      if (c.approve_hidden_reason && p.state === "pending") out.push(Notes([{ level: c.has_critical ? "critical" : "info", text: c.approve_hidden_reason }], "cz-notes"));
      var counts = c.counts || {};
      var tally = h("span", { class: "cz-tally" }, ["critical", "warning", "info"].filter(function (l) { return counts[l]; }).map(function (l) { return h("span", { class: "cz-count lvl-" + l, text: String(counts[l]) }); }));
      out.push(czSection("shield", L.checks || T("Checks"), CheckList(c.checks, code ? goTo : null, L), tally));
      if (c.impact && c.impact.length) out.push(czSection("users", L.impact, h("ul", { class: "cz-bullets" }, c.impact.map(function (x) { return h("li", null, rich(x)); }))));
      if (c.diff && c.diff.length) out.push(czSection("layers", L.diff, h("div", { class: "tablewrap" }, h("table", { class: "compact cz-diff-table" },
        h("thead", null, h("tr", null, [L.field, L.property, L.before, L.after].map(function (x) { return h("th", { scope: "col", text: x || "" }); }))),
        h("tbody", null, c.diff.map(function (r) {
          return h("tr", null, h("td", { class: "primary" }, r.field_label || r.field || "—", r.field && r.field_label && r.field !== r.field_label ? h("div", { class: "cz-fn ltr", text: r.field }) : null),
            h("td", { text: r.label }), h("td", { class: "cz-before" }, rich(r.before)), h("td", { class: "cz-after" }, rich(r.after)));
        }))))));
      if (c.fields && c.fields.length) out.push(czSection("plus", L.fields, h("div", { class: "docs" }, c.fields.map(function (f) {
        return h("div", { class: "docblock" + (f.removed ? " cz-removed" : "") }, h("div", { class: "dochead" }, icon(f.removed ? "x" : "edit", 16), h("b", { text: f.label }), h("span", { class: "k ltr", text: f.fieldname })),
          h("div", { class: "facts" }, f.facts.map(function (x) { return h("div", { class: "fact" }, h("div", { class: "k", text: x.label }), h("div", { class: "val" }, rich(x.d))); })));
      }))));
      if (c.layout && c.layout.fields) out.push(czSection("layers", L.layout, h("ol", { class: "cz-layout" }, c.layout.fields.map(function (f) {
        return h("li", { class: f.new ? "new" : "" }, h("span", { class: "grow", text: f.label }), f.fieldtype ? h("span", { class: "cz-ft", text: f.fieldtype }) : null, f.new ? h("span", { class: "badge info", text: L.new || "New" }) : null);
      })), c.layout.title ? h("span", { class: "cz-sub", text: "· " + c.layout.title }) : null));
      (c.lists || []).forEach(function (l) { out.push(czSection("filter", l.title, h("div", { class: "cz-tags" }, l.items.map(function (x) { return h("span", { class: "chip static", text: x }); })))); });
      if (code) out.push(czSection("file", c.code.title || L.code, code));
      (c.codes || []).forEach(function (x) { out.push(czSection("file", x.title || L.code, CodeBlock(x, {}, L))); });
      if (c.dry_run) out.push(czSection("refresh", L.dry_run, h("div", null,
        h("div", { class: "cz-pad" }, h("div", { class: "note " + (c.dry_run.threw ? "warning" : "info") }, icon(c.dry_run.threw ? "alert" : "checkCircle", 16), h("span", null, rich(c.dry_run.threw ? (L.threw + " " + c.dry_run.message) : L.passed)))),
        c.dry_run.changed.length ? FieldGrid(c.dry_run.changed.map(function (x) { return { label: x.label, d: x.before + " → " + x.after }; })) : null), c.dry_run.document ? h("span", { class: "cz-sub ltr", text: "· " + c.dry_run.document }) : null));
      if (c.rows && c.rows.columns && c.rows.columns.length) out.push(czSection("layers", c.rows.title || L.rows, h("div", { class: "tablewrap" }, h("table", { class: "compact" },
        h("thead", null, h("tr", null, c.rows.columns.map(function (x) { return h("th", { scope: "col", text: x }); }))),
        h("tbody", null, c.rows.rows.map(function (r) { return h("tr", null, r.map(function (v) { return h("td", null, rich(v)); })); }))))));
      if (c.result && c.result.checks && c.result.checks.length) out.push(czSection("checkCircle", L.verification, CheckList(c.result.checks, null, L)));
      if (c.purpose) out.push(czSection("info", L.purpose, h("p", { class: "cz-p" }, rich(c.purpose))));
      out.push(czSection("refresh", L.undo, h("p", { class: "cz-p" }, rich(c.undo))));
      return h("div", { class: "cz" }, out);
    }
    function CzFooter() {
      var acts = (p.actions || []).filter(function (a) { return a.kind === "link" || a.kind === "message"; });
      if (!acts.length && !byKey.cancel) return null;
      return footer([p.state === "pending" ? cancelBtn() : null].concat(acts.map(function (a) {
        return h("button", { class: "btn " + (a.style === "primary" ? "primary" : a.style === "soft" ? "soft" : ""), type: "button", onclick: function () { runAction(a, {}); } }, icon(a.icon || "external", 16), a.label);
      })));
    }
    function draw() {
      clear(box);
      if (p.customization) {
        var czParts = [head(), steps(), Outcome(), Notes(p.warnings), CustomizationBody(p.customization)];
        if (p.state === "pending" && p.next_steps && p.next_steps.length) czParts.push(Impact());
        if (st.error) czParts.push(ErrorState(st.error, null));
        czParts.push(st.confirmCancel && byKey.cancel ? ConfirmReject() : CzFooter());
        czParts.push(Developer(p.developer));
        box.appendChild(Card(czParts, "cz-card"));
        setTimeout(reportSize, 0);
        return;
      }
      var blocked = !!(p.blocked && p.state === "pending");
      var pending = p.state === "pending";
      var parts = [head(), steps(), Outcome(), Notes(blocked ? [] : p.warnings)];
      if (st.editing && p.editable) parts.push(EditForm());
      else {
        var hasBody = (p.fields && p.fields.length) || (p.items && p.items.length) || (p.documents && p.documents.length);
        if (blocked) parts.push(BlockedBody());
        else if (hasBody) parts.push(ReadyBody());   // a finished request shows its outcome, not an empty facts grid
        if (pending && !blocked) parts.push(Impact());
        if (p.impact && !blocked && pending) parts.push(h("div", { class: "msg", text: p.impact }));
        parts.push(Details());
        if (st.error) parts.push(ErrorState(st.error, null));
        if (st.confirmCancel && byKey.cancel) parts.push(ConfirmReject());
        else if (!st.picking) {
          var open = byKey.open;
          if (pending && (byKey.approve || byKey.cancel || byKey.edit || blocked)) {
            if (blocked) parts.push(footer([cancelBtn(), editBtn(T("Edit quantity")),
              byKey.choose_warehouse ? h("button", { class: "btn", type: "button", disabled: st.busy, onclick: function () { st.picking = true; draw(); } }, icon("refresh", 16), T("Choose another warehouse")) : null,
              byKey.view_stock ? h("button", { class: "btn primary grow", type: "button", onclick: function () { drill(byKey.view_stock.tool, byKey.view_stock.arguments); } }, icon("box", 16), T("View stock")) : null]));
            else parts.push(footer([detailsBtn(), cancelBtn(), editBtn(),
              byKey.approve ? h("button", { class: "btn success grow", type: "button", disabled: st.busy, onclick: function () { submit(byKey.approve.tool, byKey.approve.arguments); } }, icon("check", 16), st.busy ? T("Executing…") : byKey.approve.label) : null]));
          } else if (open) parts.push(footer([h("button", { class: "btn primary", type: "button", onclick: function () { openLink(open.href); } }, icon("external", 16), T("Open in ERP"))]));
          else if (pending) parts.push(h("div", { class: "msg", text: T("To approve, type “approve” in the chat; to reject, type “reject”.") }));
        }
      }
      parts.push(Insights(p.insights), Developer(p.developer));
      box.appendChild(Card(parts));
      setTimeout(reportSize, 0);
    }
    draw();
    return box;
  }

  /* ---------- choice card: the assistant asks, the user picks (e.g. which company) ---------- */
  function Choice(p, opts) {
    var chosen = p.options.length === 1 ? p.options[0].value : null, sent = false;
    var box = h("div", { class: "ag-stack" });
    function pick(o) { if (!sent) { chosen = o.value; draw(); } }
    function send() {
      var o = p.options.filter(function (x) { return x.value === chosen; })[0];
      if (!o) return;
      var text = p.reply_template.replace("{label}", o.label).replace("{value}", o.value);
      sent = true;
      tellModel(text);
      if (!Host.followUp(text)) { copyText(text); flash(T("Copy this reply and send it in the chat")); }
      draw();
    }
    function draw() {
      clear(box);
      var list = h("div", { class: "altlist", role: "radiogroup", "aria-label": p.title }, p.options.map(function (o) {
        return h("button", { class: "opt", type: "button", role: "radio", "aria-checked": String(chosen === o.value), disabled: sent, onclick: function () { pick(o); } },
          h("span", { class: "radio" }), o.icon ? icon(o.icon, 18) : null, h("span", { class: "grow", text: o.label }),
          o.meta ? h("span", { class: "meta", text: o.meta }) : null, o.badge ? h("span", { class: "badge " + o.badge.tone, text: o.badge.d }) : null);
      }));
      box.appendChild(Card([HeadBlock(p, opts, []), Notes(p.warnings), list,
        sent ? h("div", { class: "msg", role: "status" }, icon("checkCircle", 16), " " + T("Your choice was sent.")) :
          h("div", { class: "ag-foot" }, h("button", { class: "btn", type: "button", onclick: function () { Host.followUp(T("Cancel")) || back(); }, text: p.cancel_label || T("Cancel") }),
            h("button", { class: "btn primary grow", type: "button", disabled: !chosen, onclick: send }, p.submit_label || T("Continue"), icon(isRtl() ? "back" : "next", 16)))]));
    }
    draw();
    return box;
  }
