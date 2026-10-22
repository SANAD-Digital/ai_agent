/**
 * agent-workspace — Desk page entry.
 *
 * Responsibility: create the Frappe app page, load the chat bundles
 * (chat.bundle.css / chat.bundle.js, built from public/js/chat/*), then mount `window.AIChat`.
 * Everything else (state, transport, rendering) lives in those modules.
 *
 * Route options `{prompt, agent, company, auto_submit}` are honoured so another
 * page can hand a prompt over with `frappe.set_route("agent-workspace", {...})`.
 */

const AI_CHAT_BUNDLES = ["chat.bundle.css", "chat.bundle.js"];

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
    // backdrop, which flashed and made the page shake. Fallback: frappe.require
    // (it resolves the bundle names; the stylesheet is the LTR build either way).
    if (window.aiAgentAssets) {
        await window.aiAgentAssets.load(AI_CHAT_BUNDLES);
    } else {
        await frappe.require(AI_CHAT_BUNDLES);
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
