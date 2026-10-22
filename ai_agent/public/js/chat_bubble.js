/**
 * chat_bubble.js — loader for the floating support chat (every Desk page).
 * Tiny and dependency-free: asks ai_agent.chat.support.bootstrap once after
 * idle, draws the FAB, and lazy-loads the chat bundles (chat.bundle.css/js) on first open.
 */
(function () {
    "use strict";

    var KEY = "ai_agent:bubble_open";
    var BUNDLES = ["chat.bundle.css", "chat.bundle.js"];
    // the Musaed launcher robot (musaed-brand/components/chat-widget-demo.html): white head on the clay button
    var ROBOT = '<svg class="acb-icon acb-robot ms-robot" data-state="idle" viewBox="0 0 100 100" aria-hidden="true" focusable="false">' +
        '<g transform="translate(50 50) scale(0.95) translate(-50 -42)"><g class="ms-body">' +
        '<line x1="50" y1="30" x2="50" y2="20" stroke="#FFFFFF" stroke-width="5" stroke-linecap="round"/>' +
        '<circle class="ms-ball" cx="50" cy="18" r="5.5" fill="var(--ms-petrol-700)"/>' +
        '<rect x="20" y="44" width="7" height="12" rx="3.5" fill="#FFFFFF"/><rect x="73" y="44" width="7" height="12" rx="3.5" fill="#FFFFFF"/>' +
        '<rect x="26" y="30" width="48" height="40" rx="14" fill="#FFFFFF"/>' +
        '<rect x="32" y="38" width="36" height="22" rx="11" fill="var(--ms-petrol-700)"/>' +
        '<g class="ms-eyes ms-fb"><rect class="ms-eye ms-fb" x="39" y="43" width="7" height="12" rx="3.5" fill="var(--ms-clay-500)"/>' +
        '<rect class="ms-eye ms-fb" x="54" y="43" width="7" height="12" rx="3.5" fill="var(--ms-clay-500)"/></g>' +
        "</g></g></svg>";
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
     * Insert one bundle the way frappe.require does, without its frappe.dom.freeze()
     * (the #freeze backdrop flickered before the panel showed). The name resolves to
     * its hashed file; the stylesheet is always the LTR build, which the chat CSS is
     * written for (logical properties). The path goes into frappe.assets._executed so
     * a frappe.require of the same bundle never runs it twice.
     */
    var inflight = {}; // path -> promise, so the bubble and the page never insert a file twice

    function insert(name) {
        var assets = frappe.assets;
        var path = assets && assets.bundled_asset ? assets.bundled_asset(name, false) : name;
        if (assets && assets._executed && assets._executed.indexOf(path) !== -1) return Promise.resolve();
        if (inflight[path]) return inflight[path];
        var url = path;
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
                reject(new Error("ai_agent: failed to load " + name));
            };
            document.head.appendChild(el);
        });
        return inflight[path];
    }

    /** Shared with the agent-workspace page: load the chat bundles without the #freeze flicker. */
    window.aiAgentAssets = {
        BUNDLES: BUNDLES,
        load: function (names) {
            return Promise.all((names || BUNDLES).map(insert));
        },
    };

    /** Load the chat bundles; mount once they have run. */
    function load() {
        if (loading) return loading;
        loading = (async function () {
            await window.aiAgentAssets.load();
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
        var label = boot.title || __("Assistant", null, "AI Chat");
        fab = document.createElement("button");
        fab.type = "button";
        fab.id = "ai-chat-bubble-fab";
        fab.className = "acb-fab ms-launcher";
        fab.title = label;
        fab.setAttribute("aria-label", label);
        fab.setAttribute("aria-haspopup", "dialog");
        fab.setAttribute("aria-expanded", "false");
        fab.innerHTML = ROBOT + svg("acb-icon acb-icon-down", ICON_DOWN) + '<span class="acb-dot"></span>';
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
        // The bundle can reach a site that does not have ai_agent (a stale hooks cache after an
        // uninstall, shared assets): boot.versions lists only this site's installed apps.
        if (!f.boot.versions || !("ai_agent" in f.boot.versions)) return;
        if (window.requestIdleCallback) window.requestIdleCallback(bootstrap, { timeout: 4000 });
        else setTimeout(bootstrap, 1500);
    }

    if (document.readyState === "complete") start();
    else window.addEventListener("load", start);
})();
