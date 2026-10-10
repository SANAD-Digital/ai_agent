/**
 * message.js — user and assistant message components.
 *
 * Responsibility: `AIChat.message.render(msg, ctx)` → `{ el, update(msg),
 * destroy() }`. A user message is a light bubble with attachment chips and a
 * hover bar (edit / copy, inline edit form). An assistant message has an
 * avatar row, the block list (reconciled in place through blocks.js so
 * streaming deltas patch DOM instead of re-creating it), a pulsing "يعمل…"
 * indicator with an elapsed timer, an error state with retry, and a footer
 * with actions (copy / regenerate / 👍 / 👎) and meta (model · tokens · time).
 *
 * ctx: { onEdit(msg, text), onRetry(msg), onFeedback(msg, value), agentName, api, store }
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, __, fmtTokens, fmtDuration, fmtBytes, copyText, toast } = AIChat;

    function iconBtn(name, label, onClick, cls) {
        const b = h("button.ac-icon-btn", { type: "button", title: label, aria: { label }, class: cls }, icon(name));
        b.addEventListener("click", (e) => {
            e.stopPropagation();
            onClick(e, b);
        });
        return b;
    }

    /* ------------------------------------------------------------------ */
    /* user                                                                */
    /* ------------------------------------------------------------------ */

    function renderUser(msg, ctx) {
        const textEl = h("div.ac-bubble-text");
        const files = h("div.ac-attachments");
        const bubble = h("div.ac-bubble", files, textEl);
        const editor = h("div.ac-edit");
        editor.hidden = true;

        const actions = h("div.ac-msg-actions");
        actions.appendChild(iconBtn("copy", __("نسخ"), () => copyText(current.text).then(() => toast(__("تم النسخ")))));
        if (ctx.onEdit) {
            actions.appendChild(iconBtn("edit", __("تعديل"), () => openEditor()));
        }

        const root = h("article.ac-msg.ac-msg-user", { dataset: { name: msg.name } }, h("div.ac-msg-body", bubble, editor, actions));
        let current = msg;

        function openEditor() {
            AIChat.clear(editor);
            const ta = h("textarea.ac-textarea", { rows: 3 });
            ta.value = current.text || "";
            const save = h("button.ac-btn.ac-btn-primary", { type: "button", text: __("إرسال") });
            const cancel = h("button.ac-btn.ac-btn-ghost", { type: "button", text: __("إلغاء") });
            const close = () => {
                editor.hidden = true;
                bubble.hidden = false;
                root.classList.remove("is-editing");
            };
            cancel.addEventListener("click", close);
            save.addEventListener("click", () => {
                const text = ta.value.trim();
                if (!text || text === current.text) return close();
                save.disabled = true;
                Promise.resolve(ctx.onEdit(current, text))
                    .then(close)
                    .catch((e) => {
                        save.disabled = false;
                        editor.appendChild(h("div.ac-inline-error", { text: AIChat.errorMessage(e) }));
                    });
            });
            ta.addEventListener("keydown", (e) => {
                if (e.key === "Escape") {
                    e.preventDefault();
                    close();
                } else if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
                    e.preventDefault();
                    save.click();
                }
            });
            editor.append(ta, h("div.ac-row.ac-edit-actions", save, cancel));
            editor.hidden = false;
            bubble.hidden = true;
            root.classList.add("is-editing");
            ta.focus();
            ta.setSelectionRange(ta.value.length, ta.value.length);
            ta.style.height = "auto";
            ta.style.height = Math.min(ta.scrollHeight + 2, window.innerHeight * 0.4) + "px";
        }

        let lastKey = null;
        function update(m) {
            current = m;
            const key = JSON.stringify([m.text, m.attachments]);
            if (key === lastKey) return;
            lastKey = key;
            textEl.textContent = m.text || "";
            AIChat.clear(files);
            (m.attachments || []).forEach((f) => {
                files.appendChild(
                    h(
                        "a.ac-chip.ac-chip-file",
                        { href: f.file_url || "#", target: "_blank", rel: "noopener", title: f.file_name },
                        icon("file"),
                        h("span.ac-chip-label", { text: f.file_name || f.name }),
                        f.file_size ? h("span.ac-chip-meta", { text: fmtBytes(f.file_size) }) : null
                    )
                );
            });
            files.hidden = !(m.attachments && m.attachments.length);
        }

        update(msg);
        return { el: root, update, destroy() {}, openEditor };
    }

    /* ------------------------------------------------------------------ */
    /* assistant                                                           */
    /* ------------------------------------------------------------------ */

    function renderAssistant(msg, ctx) {
        const blocksEl = h("div.ac-blocks");
        const workingLabel = h("span", { text: __("يعمل…") });
        const timerEl = h("span.ac-working-timer");
        const working = h("div.ac-working", h("span.ac-pulse"), workingLabel, timerEl);
        const errorText = h("span.ac-error-text");
        const errorRetry = h("button.ac-btn.ac-btn-ghost", { type: "button" }, icon("refresh"), h("span", { text: __("إعادة المحاولة") }));
        // Technical detail (provider message, codes) only reaches System Managers; folded by default.
        const errorDetailText = h("pre.ac-error-detail-text");
        const errorDetail = h("details.ac-error-detail", h("summary", { text: __("تفاصيل تقنية") }), errorDetailText);
        const errorBox = h("div.ac-block.ac-error.ac-msg-error", icon("alert"), h("div.ac-error-main", errorText, errorDetail), errorRetry);
        const cancelledNote = h("div.ac-status-line", { text: __("أُوقف التوليد") });
        const awaitingNote = h("div.ac-status-line.ac-awaiting", icon("shield"), h("span", { text: __("بانتظار قرارك على طلب الاعتماد") }));

        const meta = h("span.ac-msg-meta");
        const btnCopy = iconBtn("copy", __("نسخ"), () => copyText(current.text || "").then(() => toast(__("تم النسخ"))));
        const btnRetry = iconBtn("refresh", __("إعادة التوليد"), () => ctx.onRetry && ctx.onRetry(current));
        const btnUp = iconBtn("thumbs-up", __("رد مفيد"), () => feedback(1));
        const btnDown = iconBtn("thumbs-down", __("رد غير مفيد"), () => feedback(-1));
        const footer = h("div.ac-msg-footer", h("div.ac-msg-actions", btnCopy, btnRetry, btnUp, btnDown), meta);

        const avatar = h("span.ac-avatar", icon("sparkles"));
        const nameEl = h("span.ac-msg-author", { text: ctx.agentName || __("المساعد") });
        const root = h(
            "article.ac-msg.ac-msg-assistant",
            { dataset: { name: msg.name } },
            h("div.ac-msg-head", avatar, nameEl),
            h("div.ac-msg-body", blocksEl, working, awaitingNote, errorBox, cancelledNote, footer)
        );

        let current = msg;
        const renderers = new Map(); // block seq → renderer
        let timer = null;

        errorRetry.addEventListener("click", () => ctx.onRetry && ctx.onRetry(current));

        function feedback(value) {
            const next = current.feedback === value ? 0 : value;
            current.feedback = next;
            paintFeedback();
            if (ctx.onFeedback) ctx.onFeedback(current, next);
        }

        function paintFeedback() {
            btnUp.classList.toggle("is-active", current.feedback === 1);
            btnDown.classList.toggle("is-active", current.feedback === -1);
        }

        function tick() {
            const start = current._started_local || Date.now();
            const s = Math.max(0, Math.round((Date.now() - start) / 1000));
            timerEl.textContent = s ? fmtDuration(s * 1000) : "";
        }

        function setTimer(on) {
            if (on && !timer) {
                tick();
                timer = setInterval(tick, 1000);
            } else if (!on && timer) {
                clearInterval(timer);
                timer = null;
            }
        }

        function blockCtx() {
            return { message: current, onRetry: ctx.onRetry ? () => ctx.onRetry(current) : null, api: ctx.api || AIChat.api, store: ctx.store || AIChat.store };
        }

        /* Thinking and tool calls between two answers form one "process" group: a single
           collapsible ("فكّر لمدة 7 ث · 3 أدوات") like Claude.ai. The answer text, artifacts,
           approvals and the tools' result views stay in the message flow. */
        const PROCESS = new Set(["thinking", "tool_use", "tool_result"]);
        const groups = new Map(); // seq of the run's first block → group

        function toolCount(n) {
            if (n === 1) return __("أداة واحدة");
            if (n === 2) return __("أداتان");
            return n <= 10 ? __("{0} أدوات", [n]) : __("{0} أداة", [n]);
        }

        function createGroup() {
            const label = h("span.ac-process-label");
            const metaEl = h("span.ac-process-meta");
            const body = h("div.ac-process-body");
            const el = AIChat.blocks.collapsible([h("span.ac-process-icon", icon("brain")), label, metaEl], body, { class: "ac-block ac-process", open: false });
            return { el, body, label, metaEl, members: [] };
        }

        /** Insert `el` after `after` in `parent` (first when null); an element already placed is never moved (an iframe would reload). */
        function place(parent, el, after) {
            if (el.parentNode === parent) return;
            if (after && after.parentNode === parent) parent.insertBefore(el, after.nextSibling);
            else parent.insertBefore(el, parent.firstChild);
        }

        function paintGroup(g, m, isLast) {
            const live = (m.status === "pending" || m.status === "streaming") && isLast;
            const uses = new Set(g.members.filter((b) => b.kind === "tool_use").map((b) => b.id));
            const tools = uses.size + g.members.filter((b) => b.kind === "tool_result" && !uses.has(b.id)).length;
            const results = new Set(m.blocks.filter((b) => b.kind === "tool_result").map((b) => b.id));
            g.el.classList.toggle("is-streaming", live);
            if (live) {
                const last = g.members[g.members.length - 1];
                if (last && last.kind === "tool_use" && !results.has(last.id)) g.label.textContent = AIChat.toolLabel(last.name || "") + "…";
                else g.label.textContent = __("يفكّر…");
            } else {
                const first = AIChat.parseTs(g.members[0] && g.members[0].started_at);
                const ends = g.members.map((b) => AIChat.parseTs(b.completed_at)).filter(Boolean);
                let ms = first && ends.length ? Math.max(...ends) - first : 0;
                if (!ms) ms = g.members.reduce((t, b) => t + (b.duration_ms || 0), 0);
                const thought = g.members.some((b) => b.kind === "thinking");
                g.label.textContent = ms ? (thought ? __("فكّر لمدة {0}", [fmtDuration(ms)]) : __("عمل لمدة {0}", [fmtDuration(ms)])) : thought ? __("أفكار") : __("خطوات");
            }
            g.metaEl.textContent = tools ? " · " + toolCount(tools) : "";
        }

        /** Reconcile renderers with msg.blocks, patching in place. */
        function paintBlocks(m) {
            const seen = new Set();
            const seenGroups = new Set();
            const useById = new Map();
            m.blocks.forEach((b) => {
                if (b.kind === "tool_use" && b.id) useById.set(b.id, b.seq);
            });
            let cursor = null; // last element placed in the message flow
            let run = null; // the open process group
            let inner = null; // last element placed inside it
            let lastGroupKey = null;
            for (const block of m.blocks) {
                const isProcess = PROCESS.has(block.kind);
                // only answer text closes the group; cards (files, tables, approvals) go after it meanwhile
                if (block.kind === "text") run = null;
                if (isProcess && !run) {
                    run = groups.get(block.seq);
                    if (!run) {
                        run = createGroup();
                        groups.set(block.seq, run);
                    }
                    run.members = [];
                    place(blocksEl, run.el, cursor);
                    cursor = run.el;
                    inner = null;
                    seenGroups.add(block.seq);
                    lastGroupKey = block.seq;
                }
                if (isProcess) run.members.push(block);
                if (block.kind === "tool_result" && block.id && useById.has(block.id)) {
                    const owner = renderers.get(useById.get(block.id));
                    if (owner && owner.setResult) {
                        owner.setResult(block);
                        continue;
                    }
                }
                let r = renderers.get(block.seq);
                if (r && (r.kind !== block.kind || !!r.grouped !== isProcess)) {
                    if (r.destroy) r.destroy();
                    r.el.remove();
                    if (r.outside) r.outside.remove();
                    renderers.delete(block.seq);
                    r = null;
                }
                if (!r) {
                    r = AIChat.blocks.render(block, Object.assign(blockCtx(), { inGroup: isProcess }));
                    r.grouped = isProcess;
                    renderers.set(block.seq, r);
                } else {
                    r.update(block);
                }
                if (isProcess) {
                    place(run.body, r.el, inner);
                    inner = r.el;
                    if (r.outside) {
                        place(blocksEl, r.outside, cursor);
                        cursor = r.outside;
                    }
                } else {
                    place(blocksEl, r.el, cursor);
                    cursor = r.el;
                }
                seen.add(block.seq);
            }
            for (const [seq, r] of Array.from(renderers.entries())) {
                if (!seen.has(seq)) {
                    if (r.destroy) r.destroy();
                    r.el.remove();
                    if (r.outside) r.outside.remove();
                    renderers.delete(seq);
                }
            }
            for (const [key, g] of Array.from(groups.entries())) {
                if (!seenGroups.has(key)) {
                    g.el.remove();
                    groups.delete(key);
                } else {
                    paintGroup(g, m, key === lastGroupKey && m.blocks[m.blocks.length - 1] && PROCESS.has(m.blocks[m.blocks.length - 1].kind));
                }
            }
        }

        function update(m) {
            current = m;
            root.dataset.status = m.status;
            paintBlocks(m);

            const live = m.status === "pending" || m.status === "streaming";
            const awaiting = m.status === "awaiting_approval";
            const done = !live && !awaiting;
            working.hidden = !live;
            workingLabel.textContent = m.status === "pending" ? __("يعمل…") : __("يعمل…");
            setTimer(live);
            root.classList.toggle("is-live", live);
            root.classList.toggle("is-awaiting", awaiting);
            awaitingNote.hidden = !awaiting;

            const failed = m.status === "failed";
            errorBox.hidden = !failed;
            if (failed) errorText.textContent = m.error || __("فشل توليد الرد");
            errorDetail.hidden = !(failed && m.error_detail);
            errorDetailText.textContent = (failed && m.error_detail) || "";
            errorRetry.hidden = !ctx.onRetry;
            cancelledNote.hidden = m.status !== "cancelled";

            footer.hidden = !done;
            if (done) {
                const parts = [];
                if (m.model) parts.push(m.model);
                const u = m.usage || {};
                const tok = (u.input_tokens || 0) + (u.output_tokens || 0);
                if (tok) parts.push(__("{0} رمز", [fmtTokens(tok)]));
                if (m.duration_ms) parts.push(fmtDuration(m.duration_ms));
                if (m.completed_at) parts.push(AIChat.fmtTime(m.completed_at));
                AIChat.clear(meta);
                parts.forEach((p) => meta.appendChild(h("span", { text: p })));
                btnRetry.hidden = !ctx.onRetry;
                paintFeedback();
            }
        }

        function destroy() {
            setTimer(false);
            for (const r of renderers.values()) if (r.destroy) r.destroy();
            groups.clear();
            renderers.clear();
        }

        update(msg);
        return { el: root, update, destroy };
    }

    function render(msg, ctx) {
        return msg.role === "user" ? renderUser(msg, ctx || {}) : renderAssistant(msg, ctx || {});
    }

    AIChat.message = { render };
})();
