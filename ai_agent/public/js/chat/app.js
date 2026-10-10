/**
 * app.js — composition root: `AIChat.mount(wrapper, page)`.
 *
 * Responsibility: build the layout (sidebar | main[header | messages |
 * composer]), call `bootstrap`, wire session routing
 * (`/app/agent-workspace/<session>`), the empty state with suggestion chips
 * (the message list itself is list.js), keyboard shortcuts (Ctrl/Cmd+K new chat, Ctrl/Cmd+M memory,
 * Esc stop), theme
 * detection from `[data-theme]` with a MutationObserver, and the `--ac-top`
 * variable measured from the Frappe navbar. Handles route options
 * `{prompt, agent, company, auto_submit}`.
 */
(function () {
    "use strict";

    const AIChat = window.AIChat;
    const { h, icon, __, fmtTokens, fmtCost } = AIChat;
    const store = AIChat.store;
    const api = AIChat.api;

    const SUGGESTIONS = [
        () => __("Summarize this month's sales compared with last month"),
        () => __("Which invoices are more than 30 days overdue?"),
        () => __("Create a quotation for a new customer"),
        () => __("Explain how profit margin is calculated in the reports"),
    ];

    async function mount(wrapper, page) {
        const root = h("div.ac-root", { dataset: { dir: AIChat.isRtl() ? "rtl" : "ltr" } });
        page.main.append(root);

        measureTop();
        requestAnimationFrame(measureTop);          // after Desk finishes laying out the page
        setTimeout(measureTop, 400);
        watchTheme();

        /* ---- layout ---- */

        // the message list (reconcile, scroll stickiness, pagination) lives in list.js
        const list = AIChat.messageList.create({
            store,
            api,
            messageCtx,
            renderEmpty,
            isActive: pageVisible,
            onError: (text) => showBanner(text, "error"),
        });
        const banner = h("div.ac-banner");
        banner.hidden = true;

        const titleEl = h("button.ac-header-title", { type: "button", title: __("Rename") });
        const agentEl = h("span.ac-header-agent");
        const meter = AIChat.composer.createContextMeter();
        const usageEl = h("span.ac-header-usage");
        const btnMenu = h("button.ac-icon-btn.ac-header-menu", { type: "button", title: __("Conversations"), aria: { label: __("Conversations") } }, icon("panel"));
        const btnNewTop = h("button.ac-icon-btn.ac-header-new", { type: "button", title: __("New Conversation"), aria: { label: __("New Conversation") } }, icon("plus"));
        const header = h("header.ac-header", h("div.ac-header-start", btnMenu, h("div.ac-header-text", titleEl, agentEl)), h("div.ac-header-end", usageEl, meter.el, btnNewTop));

        const composer = AIChat.composer.create({
            boot: null,
            onSend: send,
            onStop: stop,
            onEditLast: editLast,
            upload: AIChat.transport.upload,
            listSkills: (agent) => api.list_skills({ agent }),
            searchDocuments: (txt) => api.search_documents({ txt }),
        });

        const memoryPanel = AIChat.memoryPanel ? AIChat.memoryPanel.create({ api }) : null;
        const sidebar = AIChat.sidebar.create({ api, onSelect: openSession, onNew: newChat, onMemory: () => memoryPanel && memoryPanel.toggle() });

        const main = h("main.ac-main", header, banner, list.el, composer.el);
        root.append(sidebar.el, sidebar.backdrop, main);
        if (memoryPanel) root.append(memoryPanel.backdrop, memoryPanel.el);

        btnMenu.addEventListener("click", () => sidebar.toggle());
        btnNewTop.addEventListener("click", newChat);
        titleEl.addEventListener("click", renameCurrent);

        /* ---- state wiring ---- */

        let routing = false;

        function agentName() {
            const s = store.state.current;
            const agent = s ? s.agent : store.state.composer.agent;
            const a = ((store.state.boot && store.state.boot.agents) || []).find((x) => x.name === agent);
            return a ? a.agent_name || a.name : agent || __("Assistant");
        }

        function messageCtx() {
            return { onEdit: edit, onRetry: retry, onFeedback: feedback, agentName: agentName(), api, store };
        }

        function renderEmpty() {
            return h(
                "div.ac-empty",
                h("div.ac-empty-mark", icon("sparkles")),
                h("h2.ac-empty-title", { text: __("Start a conversation") }),
                h("p.ac-empty-sub", { text: __("Ask about your data, run a report or prepare a document — {0} is ready.", [agentName()]) }),
                h(
                    "div.ac-suggestions",
                    SUGGESTIONS.map((fn) => {
                        const b = h("button.ac-suggestion", { type: "button", text: fn() });
                        b.addEventListener("click", () => {
                            composer.setText(fn());
                            composer.focus();
                        });
                        return b;
                    })
                )
            );
        }

        function paintHeader() {
            const s = store.state.current;
            titleEl.textContent = s ? s.title || __("New Conversation") : __("New Conversation");
            titleEl.disabled = !s;
            AIChat.clear(agentEl);
            agentEl.appendChild(h("span", { text: agentName() }));
            if (s && s.model) agentEl.appendChild(h("span", { text: s.model }));
            meter.update(s);
            const u = s && s.usage;
            if (u && (u.input_tokens || u.output_tokens)) {
                const total = (u.input_tokens || 0) + (u.output_tokens || 0);
                AIChat.clear(usageEl);
                usageEl.appendChild(h("span", { text: __("{0} tokens", [fmtTokens(total)]) }));
                if (u.estimated_cost) usageEl.appendChild(h("span", { text: fmtCost(u.estimated_cost) }));
                usageEl.hidden = false;
            } else usageEl.hidden = true;
            composer.setSession(s);
            // the store is shared with the support bubble: only the visible page owns the tab title
            if (pageVisible()) document.title = (s && s.title ? s.title + " · " : "") + __("AI Assistant");
        }

        function paintStreaming() {
            composer.setStreaming(store.streamingMessage());
        }

        store.on("session", paintHeader);
        store.on("sessions", () => {
            if (store.state.current) paintHeader();
        });
        store.on("streaming", paintStreaming);
        store.on("sidebar", (open) => root.classList.toggle("is-sidebar-collapsed", !open));

        /* ---- errors ---- */

        let bannerTimer = null;
        function showBanner(text, kind) {
            AIChat.clear(banner);
            const close = h("button.ac-icon-btn", { type: "button", aria: { label: __("Close") } }, icon("x"));
            close.addEventListener("click", () => (banner.hidden = true));
            banner.append(icon(kind === "error" ? "alert" : "info"), h("span", { text }), close);
            banner.dataset.kind = kind || "error";
            banner.hidden = false;
            clearTimeout(bannerTimer);
            bannerTimer = setTimeout(() => (banner.hidden = true), 8000);
        }

        /* ---- session flow ---- */

        function setRoute(sessionName) {
            if (!window.frappe || !frappe.set_route) return;
            const route = frappe.get_route ? frappe.get_route() : [];
            const cur = route && route[1];
            if ((cur || null) === (sessionName || null)) return;
            routing = true;
            try {
                if (sessionName) frappe.set_route("agent-workspace", sessionName);
                else frappe.set_route("agent-workspace");
            } finally {
                setTimeout(() => (routing = false), 0);
            }
        }

        async function openSession(name, opts) {
            opts = opts || {};
            if (!name) return newChat();
            if (store.state.current && store.state.current.name === name && !opts.force) {
                composer.focus();
                return;
            }
            let session = store.getSession(name);
            try {
                if (!session) {
                    const r = await api.get_session({ session: name });
                    session = r.session;
                    store.upsertSession(session);
                }
                store.setCurrent(session);
                if (!opts.noRoute) setRoute(name);
                list.setLoading(true);
                const r = await api.get_messages({ session: name, limit: 40 });
                if (!store.state.current || store.state.current.name !== name) return;
                store.setMessages(r.messages || [], r.has_more);
                list.paintEarlier();
                list.scrollToBottom();
                composer.focus();
            } catch (e) {
                showBanner(AIChat.errorMessage(e), "error");
                if (!store.getSession(name)) newChat();
            } finally {
                list.setLoading(false);
            }
        }

        function newChat() {
            store.setCurrent(null);
            setRoute(null);
            list.paintEarlier();
            composer.focus();
        }

        async function send(payload) {
            const current = store.state.current;
            const args = {
                text: payload.text,
                session: current ? current.name : null,
                agent: current ? null : payload.agent,
                company: current ? null : payload.company,
                provider: payload.provider,
                model: payload.model,
                thinking: payload.thinking,
                files: payload.files,
                skill: payload.skill,
                context_doctype: payload.context_doctype,
                context_name: payload.context_name,
            };
            try {
                const r = await api.send_message(args);
                if (!current || current.name !== r.session.name) {
                    store.upsertSession(r.session);
                    store.setCurrent(r.session);
                    setRoute(r.session.name);
                } else {
                    store.upsertSession(r.session);
                }
                store.upsertMessage(r.user_message);
                store.upsertMessage(r.assistant_message);
                list.scrollToBottom();
            } catch (e) {
                showBanner(AIChat.errorMessage(e), "error");
                throw e;
            }
        }

        async function stop(msg) {
            const target = msg || store.streamingMessage();
            if (!target) return;
            try {
                await api.stop_message({ message: target.name });
            } catch (e) {
                showBanner(AIChat.errorMessage(e), "error");
            }
        }

        async function retry(msg) {
            if (!msg || msg.role !== "assistant") return;
            try {
                const r = await api.retry_message({ message: msg.name });
                store.removeMessage(msg.name);
                store.upsertMessage(r.assistant_message);
                list.scrollToBottom();
            } catch (e) {
                showBanner(AIChat.errorMessage(e), "error");
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
            if (!last) return;
            const r = list.getRenderer(last.name);
            if (r && r.openEditor) {
                r.openEditor();
                r.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
            }
        }

        function feedback(msg, value) {
            api.feedback({ message: msg.name, value }).catch((e) => showBanner(AIChat.errorMessage(e), "error"));
        }

        function renameCurrent() {
            const s = store.state.current;
            if (!s) return;
            const input = h("input.ac-rename-input.ac-header-rename", { type: "text", value: s.title || "", aria: { label: __("Conversation title") } });
            titleEl.replaceWith(input);
            input.focus();
            input.select();
            const finish = (save) => {
                input.replaceWith(titleEl);
                const title = input.value.trim();
                if (save && title && title !== s.title) {
                    api.rename_session({ session: s.name, title })
                        .then((r) => store.upsertSession((r && r.session) || { name: s.name, title }))
                        .catch((e) => showBanner(AIChat.errorMessage(e), "error"));
                }
            };
            input.addEventListener("keydown", (e) => {
                if (e.key === "Enter") finish(true);
                else if (e.key === "Escape") finish(false);
            });
            input.addEventListener("blur", () => finish(true));
        }

        /* ---- keyboard ---- */

        function pageVisible() {
            // the page stays in the DOM while other routes are shown and the store is shared with the bubble
            const r = frappe.get_route ? frappe.get_route() : null;
            return root.isConnected && root.offsetParent !== null && (!r || r[0] === "agent-workspace");
        }
        document.addEventListener("keydown", (e) => {
            if (!pageVisible()) return;
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && !e.shiftKey) {
                e.preventDefault();
                newChat();
            } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "m" && !e.shiftKey && memoryPanel) {
                e.preventDefault();
                memoryPanel.toggle();
            } else if (e.key === "Escape" && store.state.streaming.size && !e.defaultPrevented) {
                const inComposer = composer.el.contains(document.activeElement);
                if (!inComposer) stop();
            }
        });

        /* ---- bootstrap ---- */

        try {
            const boot = await api.bootstrap();
            store.setBoot(boot);
            if (boot.user && boot.user.dir) root.dataset.dir = boot.user.dir;
            composer.setSession(null);
            if (frappe.route_options && frappe.route_options.company) composer.setCompany(frappe.route_options.company);
        } catch (e) {
            showBanner(AIChat.errorMessage(e), "error");
        }
        AIChat.transport.subscribe();
        store.setSidebarOpen(!window.matchMedia("(max-width: 991.98px)").matches);
        fitKeyboard();
        if (window.visualViewport) {
            window.visualViewport.addEventListener("resize", fitKeyboard);
            window.visualViewport.addEventListener("scroll", fitKeyboard);
        }
        paintHeader();
        list.refresh();

        const route = frappe.get_route ? frappe.get_route() : [];
        if (route && route[1]) await openSession(route[1], { noRoute: true });
        else composer.focus();

        /* The on-screen keyboard only shrinks the visual viewport, so 100dvh
           left the composer under it and the user had to scroll to find it.
           On touch devices the page height follows the visible area instead. */
        function fitKeyboard() {
            const vv = window.visualViewport;
            if (!vv || !AIChat.isTouch()) return;
            const el = list.messagesEl;
            const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
            root.style.setProperty("--ac-vh", Math.round(vv.height) + "px");
            if (wasAtBottom) list.scrollToBottom();
        }

        /* ---- public handle ---- */

        function onShow() {
            measureTop();
            const r = frappe.get_route ? frappe.get_route() : [];
            const wanted = r && r[1] ? r[1] : null;
            const cur = store.state.current ? store.state.current.name : null;
            if (!routing && wanted !== cur) {
                // also the path back from the support bubble, which borrows the shared store elsewhere
                if (wanted) openSession(wanted, { noRoute: true });
                else newChat();
            }
            paintHeader();
            list.refresh();
            composer.focus();
        }

        function handleRouteOptions(opts) {
            if (!opts) return;
            if (opts.agent) composer.setAgent(opts.agent);
            if (opts.company) composer.setCompany(opts.company);
            if (opts.prompt) {
                if (store.state.current) newChat();
                composer.setText(String(opts.prompt));
                composer.focus();
                if (opts.auto_submit && String(opts.auto_submit) !== "0") composer.submit().catch(() => {});
            }
        }

        return { root, composer, sidebar, memoryPanel, list, onShow, handleRouteOptions, openSession, newChat };
    }

    /* ------------------------------------------------------------------ */
    /* environment                                                         */
    /* ------------------------------------------------------------------ */

    /* The chat fills the window below whatever Desk draws above it. Desk v16
       has no .navbar; above the page there can be an announcement banner, a
       page head, a breadcrumb bar — and the banner can be closed. So measure
       where the chat actually starts (document coordinates), not a navbar. */
    let topObserver = null;
    function measureTop() {
        const root = document.querySelector(".ac-root");
        let top = 0;
        if (root) {
            top = Math.round(root.getBoundingClientRect().top + window.scrollY);
        } else {
            const nav = document.querySelector(".navbar, header.navbar");
            top = nav ? Math.round(nav.getBoundingClientRect().height) : 0;
        }
        const current = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--ac-top"), 10);
        if (current !== top) document.documentElement.style.setProperty("--ac-top", Math.max(0, top) + "px");
        if (root && !topObserver && window.ResizeObserver) {
            // Re-measure when anything above the chat changes height (banner closed, page head toggled).
            topObserver = new ResizeObserver(AIChat.debounce(measureTop, 50));
            let el = root;
            while (el && el.parentElement && el !== document.body) {
                for (const sib of el.parentElement.children) if (sib !== el) topObserver.observe(sib);
                el = el.parentElement;
            }
        }
    }

    let themeObserver = null;
    function watchTheme() {
        const read = () => store.setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light");
        read();
        if (themeObserver) return;
        themeObserver = new MutationObserver(read);
        themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-theme-mode"] });
        window.addEventListener("resize", AIChat.debounce(measureTop, 150));
    }

    AIChat.mount = mount;
})();
