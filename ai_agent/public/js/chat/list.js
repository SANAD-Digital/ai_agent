/**
 * list.js — the reusable message list: `AIChat.messageList.create(opts)`.
 *
 * Responsibility: everything between the header and the composer, shared by
 * the workspace page (app.js) and the support bubble (bubble.js): reconcile
 * message renderers with `store.state.order` in place, keep each renderer
 * subscribed to its `message:<name>` topic, stick to the bottom only while
 * the user is at the bottom (otherwise show the "↓ الأحدث" pill), "تحميل
 * الأقدم" pagination, and an optional empty-state node supplied by the host.
 *
 * The store is a singleton, so several lists can exist at once (the page is
 * kept in the DOM while the bubble is open elsewhere). A list only paints
 * while `opts.isActive()` is true; call `refresh()` when it becomes active.
 *
 * opts: { store, api, messageCtx() → ctx for message.render, renderEmpty() → Node|null,
 *         showEmpty() → bool, isActive() → bool, onError(text) }
 * returns: { el, messagesEl, refresh(), refreshEmpty(), scrollToBottom(), paintEarlier(),
 *            setLoading(bool), getRenderer(name), destroy() }
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, __ } = AIChat;

    function create(opts) {
        const store = opts.store || AIChat.store;
        const api = opts.api || AIChat.api;
        const isActive = opts.isActive || (() => true);
        const showEmpty = opts.showEmpty || (() => !store.state.current && !store.state.order.length);
        const onError = opts.onError || (() => {});

        const inner = h("div.ac-messages-inner");
        const messagesEl = h("div.ac-messages", { role: "log", aria: { live: "polite" } }, inner);
        const pill = h("button.ac-scroll-pill", { type: "button" }, icon("arrow-down"), h("span", { text: __("Latest") }));
        pill.hidden = true;
        const el = h("div.ac-messages-wrap", messagesEl, pill);

        const renderers = new Map(); // message name → { r, off }
        let atBottom = true;
        let emptyEl = null;
        let earlierBtn = null;
        let loadingEarlier = false;
        let scrollQueued = false;

        /* ---- scrolling ---- */

        function isAtBottom() {
            return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
        }

        function scheduleScroll(force) {
            if (force) atBottom = true;
            if (scrollQueued) return;
            scrollQueued = true;
            requestAnimationFrame(() => {
                scrollQueued = false;
                if (atBottom) {
                    messagesEl.scrollTop = messagesEl.scrollHeight;
                    pill.hidden = true;
                } else if (store.state.streaming.size || !isAtBottom()) {
                    pill.hidden = isAtBottom();
                }
            });
        }

        messagesEl.addEventListener("scroll", () => {
            atBottom = isAtBottom();
            if (atBottom) pill.hidden = true;
        }, { passive: true });

        pill.addEventListener("click", () => {
            atBottom = true;
            messagesEl.scrollTo({ top: messagesEl.scrollHeight, behavior: "smooth" });
            pill.hidden = true;
        });

        /* ---- empty state / pagination ---- */

        function paintEmpty(rebuild) {
            if (emptyEl && (rebuild || !showEmpty())) {
                emptyEl.remove();
                emptyEl = null;
            }
            if (emptyEl || !showEmpty() || !opts.renderEmpty) return;
            emptyEl = opts.renderEmpty();
            if (emptyEl) inner.appendChild(emptyEl);
        }

        function paintEarlier() {
            if (earlierBtn) {
                earlierBtn.remove();
                earlierBtn = null;
            }
            if (!store.state.hasMore || !store.state.current) return;
            earlierBtn = h("button.ac-btn.ac-btn-ghost.ac-earlier", { type: "button" }, icon("arrow-down", "ac-rotate"), h("span", { text: __("Load older") }));
            earlierBtn.addEventListener("click", loadEarlier);
            inner.insertBefore(earlierBtn, inner.firstChild);
        }

        async function loadEarlier() {
            if (loadingEarlier || !store.state.current) return;
            const first = store.getMessage(store.state.order[0]);
            if (!first) return;
            loadingEarlier = true;
            if (earlierBtn) earlierBtn.disabled = true;
            const prevHeight = messagesEl.scrollHeight;
            try {
                const r = await api.get_messages({ session: store.state.current.name, before_seq: first.seq, limit: 40 });
                store.prependMessages(r.messages || [], r.has_more);
                paintEarlier();
                requestAnimationFrame(() => {
                    messagesEl.scrollTop += messagesEl.scrollHeight - prevHeight;
                });
            } catch (e) {
                onError(AIChat.errorMessage(e));
            } finally {
                loadingEarlier = false;
                if (earlierBtn) earlierBtn.disabled = false;
            }
        }

        /* ---- reconcile ---- */

        function drop(name) {
            const entry = renderers.get(name);
            if (!entry) return;
            entry.off();
            entry.r.destroy();
            entry.r.el.remove();
            renderers.delete(name);
        }

        /** Reconcile message renderers with store.order; `sync` also re-applies each message. */
        function paintMessages(sync) {
            if (!isActive()) return;
            const order = store.state.order;
            const seen = new Set(order);
            for (const name of Array.from(renderers.keys())) if (!seen.has(name)) drop(name);
            let cursor = earlierBtn;
            order.forEach((name) => {
                const msg = store.getMessage(name);
                if (!msg) return;
                let entry = renderers.get(name);
                if (!entry) {
                    const r = AIChat.message.render(msg, opts.messageCtx ? opts.messageCtx() : {});
                    const off = store.on("message:" + name, (m) => {
                        if (!isActive()) return;
                        r.update(m);
                        scheduleScroll();
                    });
                    entry = { r, off };
                    renderers.set(name, entry);
                    if (cursor && cursor.nextSibling) inner.insertBefore(r.el, cursor.nextSibling);
                    else if (cursor) inner.appendChild(r.el);
                    else inner.insertBefore(r.el, inner.firstChild);
                } else if (sync === true) {
                    entry.r.update(msg);
                }
                cursor = entry.r.el;
            });
            paintEmpty();
            scheduleScroll();
        }

        const offMessages = store.on("messages", () => paintMessages());
        const offSession = store.on("session", () => {
            if (!isActive()) return;
            paintEmpty();
            paintEarlier();
        });

        /** Full re-sync after the list was inactive (page shown again, bubble reopened). */
        function refresh() {
            paintEarlier();
            paintMessages(true);
            paintEmpty();
        }

        function destroy() {
            offMessages();
            offSession();
            for (const name of Array.from(renderers.keys())) drop(name);
        }

        return {
            el,
            messagesEl,
            refresh,
            refreshEmpty: () => paintEmpty(true),
            scrollToBottom: () => scheduleScroll(true),
            paintEarlier,
            setLoading: (v) => messagesEl.classList.toggle("is-loading", !!v),
            getRenderer: (name) => (renderers.get(name) ? renderers.get(name).r : null),
            destroy,
        };
    }

    AIChat.messageList = { create };
})();
