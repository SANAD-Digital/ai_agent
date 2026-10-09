/**
 * chat_bubble.js — loader for the floating support chat (every Desk page).
 * Tiny and dependency-free: asks ai_agent.chat.support.bootstrap once after
 * idle, draws the FAB, and lazy-loads chat.css + the chat modules on first open.
 */
(function () {
    "use strict";

    var KEY = "ai_agent:bubble_open";
    var BASE = "/assets/ai_agent/";
    // [module file, key on window.AIChat proving it is already loaded]
    var MODULES = [
        ["core", "h"], ["store", "store"], ["transport", "transport"], ["viewhost", "viewHost"], ["blocks", "blocks"],
        ["message", "message"], ["composer", "composer"], ["list", "messageList"], ["bubble", "bubble"],
    ];
    var ICON_CHAT = '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/><path d="M8 12h.01"/><path d="M12 12h.01"/><path d="M16 12h.01"/>';
    var ICON_DOWN = '<path d="m6 9 6 6 6-6"/>';
    var fab = null, boot = null, loading = null, pending = null, tries = 0;

    function remembered() {
        try {
            return window.localStorage.getItem(KEY) === "1";
        } catch (e) {
            return false;
        }
    }

    function onWorkspace() {
        var r = frappe.get_route ? frappe.get_route() : null;
        return !!(r && r[0] === "agent-workspace");
    }

    function svg(cls, paths) {
        return '<svg class="' + cls + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + "</svg>";
    }

    /**
     * Insert one asset the way frappe.require does, without its frappe.dom.freeze():
     * each require call fades the #freeze backdrop in and out, so ten of them made
     * the page flicker before the panel showed. Scripts use async=false so they
     * download in parallel but run in insertion order (the modules depend on each
     * other). The path goes into frappe.assets._executed so the agent-workspace
     * page, which still uses frappe.require, never runs a module twice.
     */
    var inflight = {}; // path -> promise, so the bubble and the page never insert a file twice

    function insert(path) {
        var assets = frappe.assets;
        if (assets && assets._executed && assets._executed.indexOf(path) !== -1) return Promise.resolve();
        if (inflight[path]) return inflight[path];
        var v = frappe.boot.developer_mode ? Date.now() : window._version_number;
        var url = path + (v ? "?v=" + encodeURIComponent(v) : "");
        var css = /\.css$/.test(path);
        inflight[path] = new Promise(function (resolve, reject) {
            var el = document.createElement(css ? "link" : "script");
            if (css) {
                el.rel = "stylesheet";
                el.href = url;
            } else {
                el.async = false;
                el.src = url;
            }
            el.onload = function () {
                if (assets && assets._executed) assets._executed.push(path);
                resolve();
            };
            el.onerror = function () {
                delete inflight[path]; // a later call may retry
                reject(new Error("ai_agent: failed to load " + path));
            };
            document.head.appendChild(el);
        });
        return inflight[path];
    }

    /** Shared with the agent-workspace page: load assets without the #freeze flicker. */
    window.aiAgentAssets = {
        load: function (paths) {
            return Promise.all(paths.map(insert));
        },
    };

    /** Load chat.css and the missing modules together; mount once all have run. */
    function load() {
        if (loading) return loading;
        loading = (async function () {
            var items = [insert(BASE + "css/chat.css")];
            for (var i = 0; i < MODULES.length; i++) {
                if (window.AIChat && window.AIChat[MODULES[i][1]]) continue;
                items.push(insert(BASE + "js/chat/" + MODULES[i][0] + ".js"));
            }
            await Promise.all(items);
            if (!window.AIChat || !window.AIChat.bubble) throw new Error("ai_agent: chat bubble failed to load");
            window.AIChat.bubble.mount(boot);
            return window.AIChat.bubble;
        })();
        loading.catch(function () {
            loading = null;
        });
        return loading;
    }

    /**
     * Run a panel action once the modules are in. While the first load is still
     * running, further clicks are dropped: each used to queue its own toggle, so
     * an even number of impatient clicks opened and closed the panel again.
     */
    function act(method) {
        if (pending) return pending;
        fab.classList.add("is-loading");
        pending = load()
            .then(function (bubble) {
                bubble[method]();
            })
            .catch(function (e) {
                console.error(e);
            })
            .then(function () {
                pending = null;
                fab.classList.remove("is-loading");
            });
        return pending;
    }

    /** Start loading on hover / keyboard focus, so the click usually finds everything ready. */
    function prefetch() {
        load().catch(function () {
            /* the click retries and reports */
        });
    }

    function syncRoute() {
        if (fab) fab.hidden = onWorkspace(); // the full chat page owns the shared store there
    }

    function render() {
        if (fab || document.getElementById("ai-chatbot-fab")) return; // old widget still active: stay out
        var label = boot.title || __("المساعد");
        fab = document.createElement("button");
        fab.type = "button";
        fab.id = "ai-chat-bubble-fab";
        fab.className = "acb-fab";
        fab.title = label;
        fab.setAttribute("aria-label", label);
        fab.setAttribute("aria-haspopup", "dialog");
        fab.setAttribute("aria-expanded", "false");
        fab.innerHTML = svg("acb-icon acb-icon-chat", ICON_CHAT) + svg("acb-icon acb-icon-down", ICON_DOWN) + '<span class="acb-dot"></span>';
        fab.addEventListener("click", function () {
            act("toggle");
        });
        fab.addEventListener("pointerenter", prefetch, { once: true });
        fab.addEventListener("focus", prefetch, { once: true });
        document.body.appendChild(fab);
        syncRoute();
        if (frappe.router && frappe.router.on) frappe.router.on("change", syncRoute);
        if (remembered() && !onWorkspace()) act("open");
    }

    function bootstrap() {
        if (document.getElementById("ai-chatbot-fab")) return;
        frappe.xcall("ai_agent.chat.support.bootstrap").then(function (r) {
            if (!r || !r.enabled || !r.can_access) return;
            boot = r;
            render();
        }).catch(function () {
            /* no FAB when the bootstrap fails */
        });
    }

    function start() {
        var f = window.frappe;
        if (!f || !f.boot || !f.xcall || !f.require) {
            if (++tries < 40) setTimeout(start, 500);
            return;
        }
        if (f.session && f.session.user === "Guest") return;
        if (window.requestIdleCallback) window.requestIdleCallback(bootstrap, { timeout: 4000 });
        else setTimeout(bootstrap, 1500);
    }

    if (document.readyState === "complete") start();
    else window.addEventListener("load", start);
})();
