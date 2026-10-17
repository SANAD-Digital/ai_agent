/**
 * agent-workspace — Desk page entry.
 *
 * Responsibility: create the Frappe app page, load the chat stylesheet and the
 * chat modules (public/js/chat/*) strictly in order, then mount `window.AIChat`.
 * Everything else (state, transport, rendering) lives in those modules.
 *
 * Route options `{prompt, agent, company, auto_submit}` are honoured so another
 * page can hand a prompt over with `frappe.set_route("agent-workspace", {...})`.
 */

const AI_CHAT_MODULES = [
    "core",
    "store",
    "transport",
    "viewhost",
    "blocks",
    "message",
    "composer",
    "sidebar",
    "memory",
    "list",
    "app",
];

frappe.pages["agent-workspace"].on_page_load = async function (wrapper) {
    // Capture the hand-over before any await: another page may have set it.
    const pending_route_options = frappe.route_options;
    frappe.route_options = null;

    frappe.ui.make_app_page({
        parent: wrapper,
        title: __("AI Assistant", null, "AI Chat"),
        single_column: true,
    });
    $(wrapper).addClass("ai-chat-page");
    // the legacy page template (agent_workspace.html) is injected as page
    // content by Frappe; it is an empty padded container we do not use.
    $(wrapper).children(".agent-workspace-container").remove();

    const page = wrapper.page;
    page.main.empty();

    // chat_bubble.js (on every Desk page) loads without frappe.require's #freeze
    // backdrop, which flashed once per file and made the page shake; the scripts
    // download together and run in order. Fallback: one frappe.require per file.
    const paths = ["/assets/ai_agent/css/chat.css"].concat(AI_CHAT_MODULES.map((name) => `/assets/ai_agent/js/chat/${name}.js`));
    if (window.aiAgentAssets) {
        await window.aiAgentAssets.load(paths);
    } else {
        for (const path of paths) await frappe.require(path);
    }

    if (!window.AIChat || typeof window.AIChat.mount !== "function") {
        page.main.html(
            `<div class="ac-boot-error">${__("The chat could not load. Rebuild the assets, then refresh the page.", null, "AI Chat")}</div>`
        );
        return;
    }

    wrapper.ai_chat = await window.AIChat.mount(wrapper, page);
    if (pending_route_options) {
        wrapper.ai_chat.handleRouteOptions(pending_route_options);
    }
};

/**
 * Show the app's workspace sidebar. Desk picks a sidebar from the route's
 * second part, which here is a conversation name (agent-workspace/<session>)
 * that no sidebar links to, so after a reload it showed none. Keep the current
 * sidebar when it already holds the chat; otherwise prefer AI.
 */
function show_ai_sidebar() {
    const sidebar = frappe.app && frappe.app.sidebar;
    if (!sidebar || !sidebar.setup || !sidebar.get_workspace_sidebars) return;
    const holders = sidebar.get_workspace_sidebars("agent-workspace");
    if (!holders.length || holders.includes(sidebar.sidebar_title)) return;
    sidebar.setup(holders.includes("AI") ? "AI" : holders[0]);
    sidebar.set_active_workspace_item();
}

frappe.pages["agent-workspace"].on_page_show = function (wrapper) {
    show_ai_sidebar();
    // on_page_show fires right after on_page_load too — before the async
    // mount has finished. In that case on_page_load owns the route options.
    if (!wrapper.ai_chat) return;
    const opts = frappe.route_options;
    frappe.route_options = null;
    wrapper.ai_chat.onShow();
    if (opts) wrapper.ai_chat.handleRouteOptions(opts);
};
