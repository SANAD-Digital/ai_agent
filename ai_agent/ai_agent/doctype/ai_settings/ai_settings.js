// This form's strings are translated in its own context, so its wording does not change
// the same words elsewhere in Desk (Frappe falls back to the plain translation).
const __s = (text, args) => __(text, args, "AI Settings");

// Warn whenever business data can leave to a non-Anthropic provider.
frappe.ui.form.on("AI Settings", {
	refresh(frm) {
		frm.set_intro("");
		if (frm.doc.provider_mode === "Own Key" && frm.doc.allow_third_party_providers) {
			frm.set_intro(
				__s("Own Key mode with third-party providers: questions, tool results and attachments are sent to the selected provider (e.g. DeepSeek, Gemini)."),
				"orange"
			);
		}
		render_mcp_guide(frm);
	},
	provider_mode(frm) {
		frm.trigger("refresh");
	},
	allow_third_party_providers(frm) {
		frm.trigger("refresh");
	},
	enable_mcp: render_mcp_guide,
});

const MCP_ENDPOINT_PATH = "/api/method/ai_agent.mcp.api.handle";
const MCP_SETUP = "ai_agent.mcp.setup.";
const MCP_STYLE = `
	.mcp-ui { max-width: 860px; }
	.mcp-ui .mcp-line { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	.mcp-ui .mcp-url { flex: 1; min-width: 260px; direction: ltr; text-align: left; font-family: var(--font-family-monospace, monospace); font-size: 12.5px; }
	.mcp-ui .mcp-sub { margin-top: 6px; font-size: 12.5px; color: var(--text-muted); }
	.mcp-ui .mcp-list { margin-top: 12px; border-top: 1px solid var(--border-color); }
	.mcp-ui .mcp-item { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-color); }
	.mcp-ui .mcp-item-main { flex: 1; min-width: 0; }
	.mcp-ui .mcp-item-title { font-weight: 500; color: var(--text-color); }
	.mcp-ui .mcp-item-hint { font-size: 12.5px; color: var(--text-muted); }
	.mcp-ui .mcp-tools { display: inline-flex; flex-wrap: wrap; gap: 4px; padding: 3px; border-radius: var(--border-radius-md, 8px); background: var(--control-bg); margin-bottom: 12px; }
	.mcp-ui .mcp-tools button { border: 0; background: transparent; padding: 4px 12px; border-radius: 6px; font-size: 13px; color: var(--text-muted); }
	.mcp-ui .mcp-tools button.active { background: var(--fg-color); color: var(--text-color); box-shadow: var(--shadow-sm); font-weight: 500; }
	.mcp-ui ol { padding-inline-start: 20px; margin: 0 0 12px; line-height: 1.9; }
	.mcp-ui pre { direction: ltr; text-align: left; background: var(--control-bg); border-radius: var(--border-radius-md, 8px); padding: 10px 12px; font-size: 12px; white-space: pre-wrap; word-break: break-all; margin: 0 0 12px; }
	.mcp-ui .mcp-test { margin-top: 6px; font-size: 12.5px; }
`;

// External AI tools tab: connection status with one-click fixes, connect steps per tool,
// and the apps connected to this site. All strings go through __s() (translations/ar.csv).
function render_mcp_guide(frm) {
	if (frm.is_new() || !frm.fields_dict.mcp_status_html) return;
	frappe.call(MCP_SETUP + "get_status").then((r) => draw_mcp(frm, r.message));
}

function draw_mcp(frm, st) {
	const url = frappe.urllib.get_base_url().replace(/\/$/, "") + MCP_ENDPOINT_PATH;
	const ui = { frm, st, url, refresh: (r) => { frm.reload_doc(); draw_mcp(frm, r.message); frappe.show_alert({ message: __s("Done"), indicator: "green" }); } };
	draw_mcp_status(ui, frm.fields_dict.mcp_status_html.$wrapper);
	draw_mcp_connect(ui, frm.fields_dict.mcp_connect_html.$wrapper);
	draw_mcp_clients(ui, frm.fields_dict.mcp_clients_html.$wrapper);
}

const mcp_wrap = (html) => `<style>${MCP_STYLE}</style><div class="mcp-ui">${html}</div>`;
const mcp_btn = (act, label, style = "default", attrs = "") => `<button class="btn btn-${style} btn-xs" data-act="${act}" ${attrs}>${label}</button>`;

function draw_mcp_status(ui, $w) {
	const { st, url } = ui;
	const c = st.checks;
	const missing = [
		["oauth", __s("Sign-in for AI tools is off"), __s("Turn on")],
		["chatgpt_origin", __s("ChatGPT is not allowed to sign in"), __s("Allow")],
		["my_role", __s("Your account has no access role"), __s("Give me access")],
	].filter(([key]) => !c[key]);

	if (!c.mcp_enabled) {
		$w.html(mcp_wrap(`
			<div class="mcp-line"><span class="indicator-pill gray">${__s("Off")}</span>
				<span class="text-muted">${__s("External AI tools cannot connect to this site.")}</span>
				${mcp_btn("fix:mcp_enabled", __s("Turn on"), "primary")}</div>`));
	} else {
		$w.html(mcp_wrap(`
			<div class="mcp-line">
				<span class="indicator-pill ${missing.length ? "orange" : "green"}">${missing.length ? __s("Setup incomplete") : __s("Ready to connect")}</span>
			</div>
			<div class="mcp-line" style="margin-top:10px">
				<input class="form-control mcp-url" readonly value="${frappe.utils.escape_html(url)}">
				${mcp_btn("copy", __s("Copy link"))}${mcp_btn("test", __s("Test connection"))}
			</div>
			<div class="mcp-test text-muted"></div>
			<div class="mcp-sub">${__s("{0} users have access.", [st.role_users])} <a href="#" class="text-primary" style="text-decoration:underline" data-act="grant">${__s("Give a user access")}</a></div>
			${missing.length ? `<div class="mcp-list">${missing.map(([key, text, fix]) => `
				<div class="mcp-item"><span class="indicator orange"></span><div class="mcp-item-main mcp-item-title">${text}</div>${mcp_btn("fix:" + key, fix)}</div>`).join("")}
				<div class="mcp-item" style="border:0">${missing.length > 1 ? mcp_btn("fix:all", __s("Fix all"), "primary") : ""}</div></div>` : ""}`));
	}
	$w.find(".mcp-url").on("focus", function () { this.select(); });
	bind_mcp_actions(ui, $w);
}

function draw_mcp_connect(ui, $w) {
	const { url, st } = ui;
	const tool = $w.data("tool") || "claude";
	const link = (href, label) => `<a class="btn btn-default btn-xs" target="_blank" rel="noopener" href="${href}">${label}</a>`;
	const tools = {
		claude: { name: "Claude", steps: [
			__s("Click <b>Open Claude settings</b>, then <b>Add custom connector</b>."),
			__s("Paste the link, click <b>Add</b>, then <b>Connect</b>."),
			__s("Sign in and click <b>Allow</b>. To approve requests later, reply <b>approve</b> in the chat."),
		], buttons: mcp_btn("copy", __s("Copy link"), "primary") + link("https://claude.ai/settings/connectors", __s("Open Claude settings")) },
		chatgpt: { name: "ChatGPT", steps: [
			__s("In ChatGPT open <b>Settings → Apps → Advanced settings</b> and turn on <b>Developer mode</b>."),
			__s("Click <b>Create app</b>, paste the link and choose <b>OAuth</b>."),
			__s("Sign in and click <b>Allow</b>."),
		], buttons: mcp_btn("copy", __s("Copy link"), "primary") + link("https://chatgpt.com", __s("Open ChatGPT")),
		note: st.checks.chatgpt_origin ? "" : __s("ChatGPT is not allowed to sign in yet. Use Fix in the status above.") },
		code: { name: "Claude Code", steps: [__s("Copy the command and run it in the terminal."), __s("In Claude Code type <b>/mcp</b> and choose the server to sign in.")],
			snippet: `claude mcp add --transport http sanad-erpnext ${url}` },
		cursor: { name: "Cursor", steps: [__s("Copy the settings into <b>~/.cursor/mcp.json</b>."), __s("Restart Cursor; it opens the sign-in page.")],
			snippet: JSON.stringify({ mcpServers: { "sanad-erpnext": { url } } }, null, 2) },
		vscode: { name: "VS Code", steps: [__s("Copy the settings into <b>.vscode/mcp.json</b>."), __s("Click <b>Start</b> above the server name and sign in.")],
			snippet: JSON.stringify({ servers: { "sanad-erpnext": { type: "http", url } } }, null, 2) },
		opencode: { name: "opencode", steps: [__s("Copy the settings into <b>opencode.json</b>."), __s("Restart opencode; it opens the sign-in page.")],
			snippet: JSON.stringify({ mcp: { "sanad-erpnext": { type: "remote", url, enabled: true } } }, null, 2) },
		other: { name: __s("Other tools"), steps: [
			__s("For tools without sign-in: open your user page and click <b>Generate Keys</b> under API access."),
			__s("In the tool, paste the link and add the header below with your key and secret."),
		], snippet: "Authorization: token API_KEY:API_SECRET",
		buttons: mcp_btn("copy", __s("Copy link")) + link(`/app/user/${encodeURIComponent(frappe.session.user)}`, __s("Open my user page")) },
	};
	const t = tools[tool];
	$w.html(mcp_wrap(`
		<div class="mcp-tools" role="tablist">${Object.entries(tools).map(([k, v]) =>
			`<button role="tab" aria-selected="${k === tool}" class="${k === tool ? "active" : ""}" data-tool="${k}">${v.name}</button>`).join("")}</div>
		${t.note ? `<div class="mcp-sub" style="color:var(--text-on-orange);margin:0 0 10px">${t.note}</div>` : ""}
		<ol>${t.steps.map((x) => `<li>${x}</li>`).join("")}</ol>
		${t.snippet ? `<pre>${frappe.utils.escape_html(t.snippet)}</pre>` : ""}
		<div class="mcp-line">${t.snippet ? mcp_btn("copy-snippet", __s("Copy"), t.buttons ? "default" : "primary") : ""}${t.buttons || ""}</div>`));
	$w.find("[data-tool]").on("click", function () { $w.data("tool", $(this).data("tool")); draw_mcp_connect(ui, $w); });
	$w.find('[data-act="copy-snippet"]').on("click", () => frappe.utils.copy_to_clipboard(t.snippet));
	bind_mcp_actions(ui, $w);
}

function draw_mcp_clients(ui, $w) {
	const esc = frappe.utils.escape_html;
	const apps = ui.st.clients;
	$w.html(mcp_wrap(apps.length
		? `<div class="mcp-list" style="margin-top:0">${apps.map((a) => `
			<div class="mcp-item">
				<div class="mcp-item-main"><div class="mcp-item-title">${esc(a.app_name)}</div>
					<div class="mcp-item-hint">${esc(a.users.join(", "))} · ${__s("Last used {0}", [frappe.datetime.comment_when(a.last_used)])}</div></div>
				${mcp_btn("revoke", __s("Disconnect"), "default", `data-client="${esc(a.client)}" data-name="${esc(a.app_name)}"`)}
			</div>`).join("")}</div>`
		: `<div class="text-muted">${__s("No app is connected yet.")}</div>`));
	bind_mcp_actions(ui, $w);
}

function bind_mcp_actions(ui, $w) {
	$w.find("[data-act]").not('[data-act="copy-snippet"]').on("click", function (e) {
		e.preventDefault();
		const act = $(this).data("act");
		if (act === "copy") return frappe.utils.copy_to_clipboard(ui.url);
		if (act === "test") return test_mcp_connection($w, ui.url);
		if (act.startsWith("fix:")) {
			return frappe.call({ method: MCP_SETUP + "fix", args: { item: act.slice(4) }, freeze: true }).then(ui.refresh);
		}
		if (act === "grant") {
			return frappe.prompt(
				{ fieldname: "user", fieldtype: "Link", options: "User", label: __s("User"), reqd: 1, get_query: () => ({ filters: { enabled: 1, user_type: "System User" } }) },
				(v) => frappe.call({ method: MCP_SETUP + "grant_role", args: { user: v.user } }).then(ui.refresh),
				__s("Give a user access"),
				__s("Give access")
			);
		}
		if (act === "revoke") {
			const $b = $(this);
			return frappe.confirm(__s("Disconnect {0}? Its users must sign in again.", [`<b>${frappe.utils.escape_html($b.data("name"))}</b>`]), () =>
				frappe.call({ method: MCP_SETUP + "revoke_client", args: { client: $b.data("client") } }).then(ui.refresh)
			);
		}
	});
}

// What an AI tool sees before signing in: the endpoint asks for sign-in (401)
// and the sign-in metadata points back to this endpoint.
async function test_mcp_connection($w, url) {
	const $out = $w.find(".mcp-test");
	$out.html(__s("Testing…"));
	try {
		const r = await fetch(url, { method: "POST", credentials: "omit", headers: { "Content-Type": "application/json" }, body: '{"jsonrpc":"2.0","id":1,"method":"ping"}' });
		const meta = await fetch("/.well-known/oauth-protected-resource" + MCP_ENDPOINT_PATH, { credentials: "omit" }).then((x) => (x.ok ? x.json() : null));
		const ok = r.status === 401 && meta && meta.resource === url;
		$out.html(ok
			? `<span class="text-success">${__s("The server responds and asks AI tools to sign in. You can connect.")}</span>`
			: `<span class="text-danger">${__s("The server did not respond as expected (status {0}). Check the items above or ask your system administrator.", [r.status])}</span>`);
	} catch (e) {
		$out.html(`<span class="text-danger">${__s("Could not reach the server.")}</span>`);
	}
}

// Frappe checks the strength of every Password field as you type; for a long API key the
// check fails on the server ("Integer exceeds 64-bit range") and the key ends up in Error Log.
frappe.ui.form.on("AI Settings", {
	refresh(frm) {
		["api_key", "web_search_api_key"].forEach((f) => frm.get_field(f)?.disable_password_checks?.());
	},
});
