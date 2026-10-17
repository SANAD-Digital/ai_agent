/**
 * bubble.js — the floating support chat panel: `AIChat.bubble`.
 *
 * Responsibility: `mount(boot)` builds the panel anchored above the FAB that
 * chat_bubble.js drew (`#ai-chat-bubble-fab`), `open()` / `close()` /
 * `toggle()` drive it. The conversation itself is the workspace chat: the same
 * store, transport, message list (list.js), message/blocks renderers and the
 * composer in compact mode. Only sending differs: it goes through
 * `ai_agent.chat.support.send_message` (agent/company/model are fixed
 * server-side) and carries the open document as context.
 *
 * The store is a singleton shared with the agent-workspace page. The bubble
 * remembers its own session (`mine`) and re-claims the store whenever it
 * becomes active; it hides itself on the workspace route, where the page
 * re-syncs the store from its own route on show.
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, __, md, relTime } = AIChat;
    const store = AIChat.store;
    const api = AIChat.api;
    const SUPPORT = "ai_agent.chat.support.";
    const OPEN_KEY = "ai_agent:bubble_open";

    let ui = null;

    function remember(open) {
        try {
            window.localStorage.setItem(OPEN_KEY, open ? "1" : "0");
        } catch (e) {
            /* private mode: not remembered */
        }
    }

    function route() {
        return (frappe.get_route ? frappe.get_route() : null) || [];
    }

    /** The saved document the user is looking at, or null. */
    function routeDocument() {
        const r = route();
        if (r[0] !== "Form" || !r[1] || !r[2]) return null;
        const name = String(r[2]);
        if (/^new-/.test(name)) return null;
        const frm = window.cur_frm;
        if (frm && frm.doctype === r[1] && frm.docname === name && typeof frm.is_new === "function" && frm.is_new()) return null;
        return { doctype: r[1], name };
    }

    function mount(boot) {
        if (ui) return ui;
        boot = boot || {};
        const fab = document.getElementById("ai-chat-bubble-fab");
        const agentName = (boot.agent && (boot.agent.agent_name || boot.agent.name)) || __("Assistant");
        const title = boot.title || __("Assistant");

        let mine = boot.session || null; // the bubble's own session
        store.state.autoApproval = !!boot.auto_approval; // the approval card's "always approve" button
        let loadedFor = null;            // session whose messages we put in the store
        let loadingMessages = false;
        let isOpen = false;
        let suppressed = route()[0] === "agent-workspace";
        let detached = null;             // "doctype::name" the user detached on this route
        let prevStreaming = new Set();
        let sending = false;             // send_message in flight: the robot stays on "processing"
        let wasLive = false;             // a reply of ours was streaming at the last paint
        let watching = null;             // [message name, unsubscribe] while a reply streams

        const active = () => isOpen && !suppressed;
        const owns = () => (store.state.current ? store.state.current.name : null) === (mine ? mine.name : null);

        /* ---- layout ---- */

        const iconBtn = (name, label, onClick) => {
            const b = h("button.ac-icon-btn", { type: "button", title: label, aria: { label } }, icon(name));
            b.addEventListener("click", (e) => {
                e.stopPropagation();
                onClick(e);
            });
            return b;
        };
        const btnNew = iconBtn("plus", __("New Conversation"), newChat);
        const btnHistory = iconBtn("clock", __("Previous conversations"), toggleHistory);
        const btnWorkspace = iconBtn("external", __("Open in the full chat"), openInWorkspace);
        const btnClose = iconBtn("x", __("Close"), close);
        btnHistory.setAttribute("aria-haspopup", "true");

        const face = AIChat.robot("ac-support-robot", "idle");
        const mood = AIChat.robotMood(face);
        const header = h(
            "header.ac-support-head",
            face,
            h("div.ac-support-titles", h("div.ac-support-title", { text: title }), h("div.ac-support-sub", { text: agentName })),
            h("div.ac-support-actions", btnNew, btnHistory, btnWorkspace, btnClose)
        );
        const history = h("div.ac-support-history", { role: "dialog", aria: { label: __("Previous conversations") } });
        history.hidden = true;

        const list = AIChat.messageList.create({
            store,
            api,
            isActive: active,
            showEmpty: () => !store.state.order.length && !loadingMessages,
            renderEmpty: renderWelcome,
            messageCtx: () => ({ onEdit: edit, onRetry: retry, onFeedback: feedback, onSend: sendText, agentName, api, store }),
            onError: (text) => composer.showError(text),
        });

        const composer = AIChat.composer.create({
            compact: true,
            pickers: false,
            listSkills: () => frappe.xcall(SUPPORT + "list_skills"),
            state: AIChat.composer.newState(),
            boot: {},
            placeholder: __("Type your question… (/ for skills and plugins)"),
            warning: boot.third_party_warning || "",
            send,
            onStop: stop,
            onEditLast: editLast,
            upload: AIChat.transport.upload,
            onContextRemoved: (c) => {
                detached = c.doctype + "::" + c.name;
                list.refreshEmpty();
            },
        });

        const root = h(
            "section.ac-root.ac-support",
            { role: "dialog", aria: { label: title, hidden: "true" }, dataset: { dir: (boot.user && boot.user.dir) || (AIChat.isRtl() ? "rtl" : "ltr") } },
            header,
            history,
            list.el,
            composer.el
        );
        document.body.appendChild(root);

        /** A question's answer or a picked suggestion: sent as if the user typed it. */
        function sendText(text) {
            if (!text) return;
            composer.setText(text);
            return composer.submit();
        }

        /* ---- welcome ---- */

        function renderWelcome() {
            const doc = routeDocument();
            const chips = (boot.suggestions || [])
                .filter((s) => s && s.label && (!s.needs_document || doc))
                .map((s) => {
                    const b = h("button.ac-suggestion", { type: "button", text: s.label });
                    b.addEventListener("click", () => {
                        composer.setText(s.label);
                        composer.submit().catch(() => {});
                    });
                    return b;
                });
            const hello = boot.welcome_message || __("Hello{0}, how can I help you today?", [boot.user && boot.user.full_name ? " " + boot.user.full_name : ""]);
            return h(
                "div.ac-support-welcome",
                h("div.ac-md.ac-support-hello", { html: md(hello) }),
                chips.length ? h("div.ac-support-chips", chips) : null
            );
        }

        /* ---- document context ---- */

        function syncContext() {
            const doc = routeDocument();
            if (doc && detached !== doc.doctype + "::" + doc.name) {
                composer.setContext({
                    doctype: doc.doctype,
                    name: doc.name,
                    sticky: true,
                    label: __("About: {0} · {1}", [__(doc.doctype), doc.name]),
                });
            } else {
                composer.setContext(null);
            }
        }

        /* ---- store ownership ---- */

        /** Make the shared store show the bubble's session (after the page, or another session, used it). */
        async function claim() {
            const want = mine ? mine.name : null;
            if (owns() && (!want || loadedFor === want)) {
                list.refresh();
                return;
            }
            store.setCurrent(mine);
            if (!want) {
                loadedFor = null;
                list.refresh();
                return;
            }
            loadingMessages = true;
            list.setLoading(true);
            list.refresh();
            try {
                const r = await api.get_messages({ session: want, limit: 40 });
                if (!mine || mine.name !== want || !owns()) return;
                loadingMessages = false;
                store.setMessages(r.messages || [], r.has_more);
                loadedFor = want;
            } catch (e) {
                composer.showError(AIChat.errorMessage(e));
            } finally {
                loadingMessages = false;
                list.setLoading(false);
                list.refresh();
                list.scrollToBottom();
                paintLive();
            }
        }

        function paintHeader() {
            btnWorkspace.hidden = !mine;
        }

        function paintLive() {
            const mineLive = owns() && store.state.streaming.size > 0;
            composer.setStreaming(owns() ? store.streamingMessage() : null);
            if (fab) fab.classList.toggle("is-working", mineLive);
            paintMood();
        }

        /**
         * The header robot follows the request: processing until the reply's
         * first words arrive, thinking while they stream, done when it lands;
         * otherwise listening while the user types, idle (then asleep) at rest.
         */
        function paintMood() {
            const msg = owns() ? store.streamingMessage() : null;
            if (!msg || (watching && watching[0] !== msg.name)) {
                if (watching) watching[1]();
                watching = null;
            }
            if (msg) {
                if (!watching) watching = [msg.name, store.on("message:" + msg.name, paintMood)];
                wasLive = true;
                return mood.set(msg.text ? "thinking" : "processing");
            }
            if (sending) return;
            if (wasLive) {
                wasLive = false;
                return mood.flash("done", 2000);
            }
            if (mood.flashing()) return;
            mood.set(composer.getText().trim() ? "listening" : "idle");
        }

        /* ---- actions ---- */

        async function send(payload) {
            if (!owns()) store.setCurrent(mine);
            sending = true;
            mood.set("processing");
            const args = {
                text: payload.text,
                session: mine ? mine.name : null,
                files: payload.files || [],
                skill: payload.skill || null,
                plugin: payload.plugin || null,
                context_doctype: payload.context_doctype || null,
                context_name: payload.context_name || null,
            };
            try {
                const r = await frappe.xcall(SUPPORT + "send_message", args);
                sending = false;
                mine = r.session;
                store.setCurrent(r.session);
                loadedFor = r.session.name;
                store.upsertMessage(r.user_message);
                store.upsertMessage(r.assistant_message);
                paintHeader();
                paintLive();
                list.scrollToBottom();
            } catch (e) {
                sending = false;
                composer.showError(AIChat.errorMessage(e));
                paintMood();
                throw e;
            }
        }

        function stop(msg) {
            const target = msg || store.streamingMessage();
            if (!target) return;
            api.stop_message({ message: target.name }).catch((e) => composer.showError(AIChat.errorMessage(e)));
        }

        async function retry(msg) {
            if (!msg || msg.role !== "assistant") return;
            try {
                const r = await api.retry_message({ message: msg.name });
                store.removeMessage(msg.name);
                store.upsertMessage(r.assistant_message);
                list.scrollToBottom();
            } catch (e) {
                composer.showError(AIChat.errorMessage(e));
            }
        }

        async function edit(msg, text) {
            const r = await api.edit_message({ message: msg.name, text });
            store.removeMessagesFrom(msg.seq);
            store.upsertMessage(r.user_message);
            store.upsertMessage(r.assistant_message);
            list.scrollToBottom();
        }

        function editLast() {
            const last = store.lastMessage("user");
            const r = last && list.getRenderer(last.name);
            if (r && r.openEditor) r.openEditor();
        }

        function feedback(msg, value) {
            api.feedback({ message: msg.name, value }).catch((e) => composer.showError(AIChat.errorMessage(e)));
        }

        function newChat() {
            closeHistory();
            mine = null;
            loadedFor = null;
            store.setCurrent(null);
            composer.showError("");
            paintHeader();
            paintLive();
            list.refresh();
            composer.focus();
        }

        function openSession(session) {
            closeHistory();
            mine = session;
            paintHeader();
            claim().then(() => composer.focus());
        }

        function openInWorkspace() {
            if (!mine) return;
            const name = mine.name;
            close();
            frappe.set_route("agent-workspace", name);
        }

        /* ---- history popover ---- */

        function closeHistory() {
            history.hidden = true;
            btnHistory.classList.remove("is-active");
        }

        function toggleHistory() {
            if (!history.hidden) return closeHistory();
            history.hidden = false;
            btnHistory.classList.add("is-active");
            AIChat.clear(history);
            history.appendChild(h("div.ac-popover-title", { text: __("Previous conversations") }));
            const body = h("div.ac-support-history-list", h("div.ac-popover-empty", icon("loader", "ac-spin")));
            history.appendChild(body);
            frappe
                .xcall(SUPPORT + "list_sessions", { limit: 20 })
                .then((r) => paintHistory(body, (r && r.sessions) || []))
                .catch((e) => {
                    AIChat.clear(body);
                    body.appendChild(h("div.ac-inline-error", { text: AIChat.errorMessage(e) }));
                });
        }

        function paintHistory(body, sessions) {
            AIChat.clear(body);
            if (!sessions.length) {
                body.appendChild(h("div.ac-popover-empty", { text: __("No previous conversations") }));
                return;
            }
            sessions.forEach((s) => {
                const main = h(
                    "button.ac-session-main",
                    { type: "button" },
                    h("span.ac-session-line", h("span.ac-session-title", { text: s.title || __("Untitled conversation") }), h("span.ac-session-time", { text: relTime(s.last_message_on) })),
                    s.preview ? h("span.ac-session-preview", { text: s.preview }) : null
                );
                const del = h("button.ac-icon-btn.ac-session-menu-btn", { type: "button", title: __("Delete"), aria: { label: __("Delete") } }, icon("trash"));
                const row = h("div.ac-session", { class: mine && mine.name === s.name ? "is-current" : null, dataset: { name: s.name } }, main, del);
                main.addEventListener("click", () => openSession(s));
                del.addEventListener("click", (e) => {
                    e.stopPropagation();
                    confirmDelete(body, row, s);
                });
                body.appendChild(row);
            });
        }

        function confirmDelete(body, row, s) {
            if (row.querySelector(".ac-confirm")) return;
            const yes = h("button.ac-btn.ac-btn-danger.ac-btn-xs", { type: "button", text: __("Delete") });
            const no = h("button.ac-btn.ac-btn-ghost.ac-btn-xs", { type: "button", text: __("Cancel") });
            const box = h("div.ac-confirm", h("span", { text: __("Delete this conversation?") }), yes, no);
            row.appendChild(box);
            row.classList.add("is-confirming");
            no.addEventListener("click", (e) => {
                e.stopPropagation();
                box.remove();
                row.classList.remove("is-confirming");
            });
            yes.addEventListener("click", (e) => {
                e.stopPropagation();
                yes.disabled = true;
                api.delete_session({ session: s.name })
                    .then(() => {
                        row.remove();
                        if (!body.querySelector(".ac-session")) paintHistory(body, []);
                        if (mine && mine.name === s.name) {
                            history.hidden = false; // keep the list open; only the conversation resets
                            mine = null;
                            loadedFor = null;
                            store.setCurrent(null);
                            paintHeader();
                            list.refresh();
                        }
                    })
                    .catch((err) => {
                        yes.disabled = false;
                        AIChat.toast(AIChat.errorMessage(err), "red");
                    });
            });
        }

        /* ---- open / close ---- */

        /* ---- phone keyboard ---- */

        // On a phone the panel is a full-screen sheet, but the on-screen keyboard
        // only shrinks the visual viewport: size the sheet to it, or the composer
        // sits under the keyboard and the user has to scroll the page to find it.
        const sheet = window.matchMedia("(max-width: 639.98px)");
        const vv = window.visualViewport;

        function fitKeyboard() {
            if (!vv || !sheet.matches || !active()) {
                root.style.removeProperty("--acs-top");
                root.style.removeProperty("--acs-height");
                return;
            }
            const el = list.messagesEl;
            const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
            root.style.setProperty("--acs-top", Math.round(vv.offsetTop) + "px");
            root.style.setProperty("--acs-height", Math.round(vv.height) + "px");
            if (wasAtBottom) list.scrollToBottom(); // keep the last message in sight as the keyboard opens
        }

        /**
         * Keep the panel's top below whatever is pinned to the top of the page
         * (Desk's sticky page head, a site's own bar). Those differ per page
         * and per site, so look at what actually sits at the top edge above
         * the panel: the nearest fixed/sticky ancestor of each element there.
         */
        function fitTop() {
            if (!active() || sheet.matches) return;
            const r = root.getBoundingClientRect();
            let bottom = 0;
            for (const el of document.elementsFromPoint((r.left + r.right) / 2, 1)) {
                if (root.contains(el)) continue;
                for (let a = el; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
                    const pos = getComputedStyle(a).position;
                    if (pos !== "fixed" && pos !== "sticky") continue;
                    const b = a.getBoundingClientRect();
                    if (b.top <= 1 && b.height < window.innerHeight / 3) bottom = Math.max(bottom, b.bottom); // not full-page overlays
                    break;
                }
            }
            root.style.setProperty("--acs-clear", Math.round(bottom) + 12 + "px");
        }
        window.addEventListener("resize", AIChat.debounce(fitTop, 100));

        if (vv) {
            vv.addEventListener("resize", fitKeyboard);
            vv.addEventListener("scroll", fitKeyboard);
        }

        function paintOpen() {
            fitKeyboard();
            fitTop();
            const shown = active();
            root.classList.toggle("is-shown", shown);
            root.setAttribute("aria-hidden", shown ? "false" : "true");
            if (fab) {
                fab.classList.toggle("is-open", shown);
                fab.setAttribute("aria-expanded", shown ? "true" : "false");
            }
        }

        function open() {
            if (isOpen && !suppressed) return composer.focus();
            isOpen = true;
            remember(true);
            if (fab) fab.classList.remove("is-unread");
            paintOpen();
            if (suppressed) return;
            if (!wasLive) mood.flash("welcome", 1500);
            syncContext();
            claim();
            requestAnimationFrame(() => composer.focus());
        }

        function close() {
            if (!isOpen) return;
            const hadFocus = root.contains(document.activeElement);
            isOpen = false;
            remember(false);
            closeHistory();
            composer.closePopovers();
            paintOpen();
            if (hadFocus && fab && !fab.hidden) fab.focus();
        }

        function toggle() {
            if (active()) close();
            else open();
        }

        /* ---- wiring ---- */

        root.addEventListener("keydown", (e) => {
            if (e.key !== "Escape" || e.defaultPrevented) return; // the composer already used it to stop
            e.preventDefault();
            e.stopPropagation();
            if (!history.hidden) closeHistory();
            else close();
        });
        document.addEventListener("click", (e) => {
            if (!history.hidden && !history.contains(e.target) && !btnHistory.contains(e.target)) closeHistory();
        });

        composer.ta.addEventListener("input", () => {
            if (!wasLive && !mood.flashing()) paintMood();
        });

        store.on("streaming", (set) => {
            // a reply that just left the streaming set while nobody was looking → unread dot
            if (owns()) {
                for (const name of prevStreaming) {
                    if (set.has(name)) continue;
                    const msg = store.getMessage(name);
                    if (msg && !active() && fab && (msg.status === "completed" || msg.status === "awaiting_approval")) fab.classList.add("is-unread");
                }
            }
            prevStreaming = new Set(set);
            paintLive();
        });
        store.on("session", (current) => {
            // session.updated for our session (title, usage) keeps `mine` fresh
            if (mine && current && current.name === mine.name) mine = current;
        });

        if (frappe.router && frappe.router.on) {
            frappe.router.on("change", () => {
                const was = suppressed;
                suppressed = route()[0] === "agent-workspace";
                detached = null;
                paintOpen();
                if (suppressed) return closeHistory();
                syncContext();
                if (isOpen && was) claim();        // back from the workspace page: take the store back
                else if (active()) list.refreshEmpty(); // welcome chips depend on the open document
            });
        }

        const themeOf = () => (document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");
        store.setTheme(themeOf());
        new MutationObserver(() => store.setTheme(themeOf())).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

        AIChat.transport.subscribe();
        paintHeader();
        paintOpen();
        syncContext();
        root.getBoundingClientRect(); // commit the hidden state, so an open() right after mount animates in

        ui = { root, composer, list, open, close, toggle, newChat, isOpen: () => active(), session: () => mine };
        return ui;
    }

    AIChat.bubble = {
        mount,
        open: () => ui && ui.open(),
        close: () => ui && ui.close(),
        toggle: () => ui && ui.toggle(),
    };
})();
