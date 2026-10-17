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
		frm.set_query("default_provider", () => ({ filters: { enabled: 1 } }));
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
// Colours from the Musaed brand tokens (--ms-*, loaded on every Desk page); surfaces
// and text from Desk's own variables so the panel sits in the form in both themes.
const MCP_STYLE = `
	.mcp-ui { max-width: 880px; color: var(--text-color); }
	.mcp-ui .mcp-card { background: var(--fg-color); border: 1px solid var(--border-color); border-radius: var(--ms-radius-md, 14px); padding: 18px 20px; }
	.mcp-ui .mcp-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
	.mcp-ui .mcp-title { font-family: var(--ms-font-brand); font-weight: 600; font-size: 16px; color: var(--ms-heading); margin: 0; }
	.mcp-ui .mcp-pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; font-size: 12.5px; font-weight: 500; }
	.mcp-ui .mcp-pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
	.mcp-ui .mcp-pill.is-ok { color: var(--green-700, #15803d); background: var(--green-50, #ecfdf3); }
	.mcp-ui .mcp-pill.is-warn { color: var(--ms-clay-700); background: var(--ms-clay-50); }
	.mcp-ui .mcp-pill.is-off { color: var(--text-muted); background: var(--control-bg); }
	.mcp-ui .mcp-label { font-size: 12.5px; color: var(--text-muted); margin-bottom: 6px; }
	.mcp-ui .mcp-urlbox { display: flex; align-items: stretch; gap: 8px; flex-wrap: wrap; }
	.mcp-ui .mcp-url { flex: 1; min-width: 260px; display: flex; align-items: center; gap: 4px; background: var(--control-bg); border-radius: 10px; padding: 0 4px 0 12px; direction: ltr; }
	.mcp-ui .mcp-url input { flex: 1; min-width: 0; border: 0; background: transparent; outline: 0; font-family: var(--font-family-monospace, monospace); font-size: 12.5px; color: var(--text-color); height: 36px; }
	.mcp-ui .mcp-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 34px; padding: 0 14px; border-radius: 10px; border: 1px solid var(--border-color); background: var(--fg-color); color: var(--text-color); font-size: 13px; font-weight: 500; white-space: nowrap; cursor: pointer; transition: background .15s, border-color .15s; }
	.mcp-ui .mcp-btn:hover { background: var(--control-bg); }
	.mcp-ui .mcp-btn:focus-visible { outline: 2px solid var(--ms-clay-500); outline-offset: 2px; }
	.mcp-ui .mcp-btn-primary { background: var(--ms-accent); border-color: var(--ms-accent); color: var(--ms-on-accent); }
	.mcp-ui .mcp-btn-primary:hover { background: color-mix(in srgb, var(--ms-clay-500) 82%, var(--ms-clay-700)); }
	.mcp-ui .mcp-btn-ghost { border-color: transparent; background: transparent; color: var(--text-muted); height: 28px; padding: 0 8px; }
	.mcp-ui .mcp-btn svg { width: 15px; height: 15px; }
	.mcp-ui .mcp-test { margin-top: 8px; font-size: 12.5px; min-height: 18px; }
	.mcp-ui .mcp-checks { margin: 16px 0 0; padding: 0; list-style: none; border-top: 1px solid var(--border-color); }
	.mcp-ui .mcp-check { display: flex; align-items: center; gap: 10px; min-height: 46px; padding: 6px 0; border-bottom: 1px solid var(--border-color); }
	.mcp-ui .mcp-check:last-child { border-bottom: 0; }
	.mcp-ui .mcp-mark { flex: 0 0 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; }
	.mcp-ui .mcp-mark svg { width: 13px; height: 13px; }
	.mcp-ui .mcp-check.is-ok .mcp-mark { background: var(--green-50, #ecfdf3); color: var(--green-700, #15803d); }
	.mcp-ui .mcp-check.is-todo .mcp-mark { background: var(--ms-clay-50); color: var(--ms-clay-700); }
	.mcp-ui .mcp-check.is-info .mcp-mark { background: var(--ms-petrol-50); color: var(--ms-petrol-700); }
	.mcp-ui .mcp-check-text { flex: 1; min-width: 0; font-size: 13.5px; }
	.mcp-ui .mcp-check.is-ok .mcp-check-text { color: var(--text-muted); }
	.mcp-ui .mcp-check a { color: var(--ms-link); font-weight: 500; }
	.mcp-ui .mcp-tools { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; }
	.mcp-ui .mcp-tool { height: 34px; padding: 0 14px; border-radius: 999px; border: 1px solid var(--border-color); background: var(--fg-color); color: var(--text-muted); font-size: 13px; cursor: pointer; }
	.mcp-ui .mcp-tool:hover { color: var(--text-color); }
	.mcp-ui .mcp-tool[aria-selected="true"] { background: var(--ms-clay-50); border-color: var(--ms-clay-300); color: var(--ms-clay-700); font-weight: 600; }
	.mcp-ui .mcp-steps { list-style: none; counter-reset: step; margin: 0 0 14px; padding: 0; }
	.mcp-ui .mcp-steps li { counter-increment: step; position: relative; padding-inline-start: 36px; min-height: 26px; margin-bottom: 10px; line-height: 1.75; font-size: 13.5px; }
	.mcp-ui .mcp-steps li::before { content: counter(step); position: absolute; inset-inline-start: 0; top: 0; width: 24px; height: 24px; border-radius: 50%; background: var(--ms-petrol-700); color: #fff; font-family: var(--ms-font-brand); font-size: 12px; font-weight: 600; display: grid; place-items: center; }
	.mcp-ui .mcp-en { unicode-bidi: isolate; direction: ltr; white-space: nowrap; }
	.mcp-ui .mcp-code { position: relative; margin: 0 0 14px; }
	.mcp-ui .mcp-code pre { direction: ltr; text-align: left; background: var(--ms-petrol-900); color: #e8f1f0; border-radius: 10px; padding: 14px 16px; padding-right: 76px; font-size: 12.5px; line-height: 1.6; white-space: pre; overflow-x: auto; margin: 0; }
	.mcp-ui .mcp-code .mcp-btn { position: absolute; top: 8px; right: 8px; height: 28px; padding: 0 10px; background: rgba(255,255,255,.08); border-color: rgba(255,255,255,.16); color: #e8f1f0; }
	.mcp-ui .mcp-note { margin: 0 0 14px; padding: 10px 12px; border-radius: 10px; background: var(--ms-clay-50); color: var(--ms-clay-900); font-size: 13px; }
	.mcp-ui .mcp-actions { display: flex; gap: 8px; flex-wrap: wrap; }
	.mcp-ui .mcp-apps { margin: 0; padding: 0; list-style: none; }
	.mcp-ui .mcp-app { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border-color); }
	.mcp-ui .mcp-app:last-child { border-bottom: 0; }
	.mcp-ui .mcp-app-mark { flex: 0 0 36px; height: 36px; border-radius: 10px; display: grid; place-items: center; background: var(--ms-petrol-50); color: var(--ms-petrol-700); font-family: var(--ms-font-brand); font-weight: 700; }
	.mcp-ui .mcp-app-main { flex: 1; min-width: 0; }
	.mcp-ui .mcp-app-name { font-weight: 600; }
	.mcp-ui .mcp-app-hint { font-size: 12.5px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.mcp-ui .mcp-empty { color: var(--text-muted); font-size: 13px; padding: 4px 0; }
	[data-theme="dark"] .mcp-ui .mcp-pill.is-ok, [data-theme="dark"] .mcp-ui .mcp-check.is-ok .mcp-mark { background: rgba(34,197,94,.14); color: #4ade80; }
	[data-theme="dark"] .mcp-ui .mcp-pill.is-warn, [data-theme="dark"] .mcp-ui .mcp-check.is-todo .mcp-mark, [data-theme="dark"] .mcp-ui .mcp-tool[aria-selected="true"], [data-theme="dark"] .mcp-ui .mcp-note { background: color-mix(in srgb, var(--ms-clay-700) 30%, transparent); color: var(--ms-clay-300); border-color: transparent; }
	[data-theme="dark"] .mcp-ui .mcp-check.is-info .mcp-mark,
	[data-theme="dark"] .mcp-ui .mcp-app-mark { background: color-mix(in srgb, var(--ms-petrol-500) 35%, transparent); color: var(--ms-petrol-100); }
	[data-theme="dark"] .mcp-ui .mcp-steps li::before { background: var(--ms-petrol-500); }
`;
const MCP_ICON = {
	copy: '<path d="M8 4h9a2 2 0 0 1 2 2v11"/><rect x="5" y="7" width="11" height="13" rx="2"/>',
	check: '<path d="M20 6 9 17l-5-5"/>',
	alert: '<path d="M12 8v5"/><path d="M12 16.5h.01"/>',
	external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
	pulse: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
	users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
};
const mcp_icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${MCP_ICON[name]}</svg>`;

// Translated steps mix Arabic with English UI names ("(Developer mode)", ".vscode/mcp.json").
// Isolate each English run so a line never breaks inside it or flips its brackets.
function mcp_bidi(html) {
	return html
		.replace(/\(([A-Za-z][^()<>]*)\)/g, '<span class="mcp-en">($1)</span>')
		.replace(/<b>([^<؀-ۿ]+)<\/b>/g, '<b class="mcp-en">$1</b>');
}

// External AI tools tab: a status card (link, test, a checklist with one-click fixes),
// connect steps per tool, and the apps connected to this site.
// All strings go through __s() (translations/ar.csv).
function render_mcp_guide(frm) {
	if (frm.is_new() || !frm.fields_dict.mcp_status_html) return;
	const agent = frm.get_field("mcp_agent");
	if (agent && agent.$input) agent.$input.attr("placeholder", "ERP Analyst");
	frappe.call(MCP_SETUP + "get_status").then((r) => draw_mcp(frm, r.message));
}

function draw_mcp(frm, st) {
	const url = frappe.urllib.get_base_url().replace(/\/$/, "") + MCP_ENDPOINT_PATH;
	const ui = {
		frm, st, url,
		refresh: (r) => {
			frm.reload_doc();
			draw_mcp(frm, r.message);
			frappe.show_alert({ message: __s("Done"), indicator: "green" });
		},
	};
	draw_mcp_status(ui, frm.fields_dict.mcp_status_html.$wrapper);
	draw_mcp_connect(ui, frm.fields_dict.mcp_connect_html.$wrapper);
	draw_mcp_clients(ui, frm.fields_dict.mcp_clients_html.$wrapper);
}

const mcp_wrap = (html) => `<style>${MCP_STYLE}</style><div class="mcp-ui">${html}</div>`;
const mcp_btn = (act, label, kind = "", attrs = "", icon = "") =>
	`<button type="button" class="mcp-btn ${kind ? "mcp-btn-" + kind : ""}" data-act="${act}" ${attrs}>${icon ? mcp_icon(icon) : ""}<span>${label}</span></button>`;

function draw_mcp_status(ui, $w) {
	const { st, url } = ui;
	const c = st.checks;

	if (!c.mcp_enabled) {
		$w.html(mcp_wrap(`<div class="mcp-card">
			<div class="mcp-head" style="margin:0">
				<span class="mcp-pill is-off">${__s("Off")}</span>
				<span style="flex:1">${__s("External AI tools cannot connect to this site.")}</span>
				${mcp_btn("fix:mcp_enabled", __s("Turn on"), "primary")}
			</div></div>`));
		return bind_mcp_actions(ui, $w);
	}

	// every prerequisite, done or not, so the admin sees the whole picture
	const checks = [
		["oauth", __s("Sign-in for AI tools is on"), __s("Sign-in for AI tools is off"), __s("Turn on")],
		["chatgpt_origin", __s("ChatGPT can sign in"), __s("ChatGPT is not allowed to sign in"), __s("Allow")],
		["my_role", __s("Your account has access"), __s("Your account has no access role"), __s("Give me access")],
	];
	const todo = checks.filter(([key]) => !c[key]).length;
	const row = (state, text, action) => `<li class="mcp-check is-${state}">
		<span class="mcp-mark">${mcp_icon({ ok: "check", todo: "alert", info: "users" }[state])}</span>
		<span class="mcp-check-text">${text}</span>${action || ""}</li>`;

	$w.html(mcp_wrap(`<div class="mcp-card">
		<div class="mcp-head">
			<h4 class="mcp-title">${__s("Server link")}</h4>
			<span class="mcp-pill ${todo ? "is-warn" : "is-ok"}">${todo ? __s("Setup incomplete") : __s("Ready to connect")}</span>
		</div>
		<div class="mcp-urlbox">
			<div class="mcp-url"><input readonly value="${frappe.utils.escape_html(url)}" aria-label="${__s("Server link")}">
				${mcp_btn("copy", __s("Copy link"), "ghost", "", "copy")}</div>
			${mcp_btn("test", __s("Test connection"), "", "", "pulse")}
		</div>
		<div class="mcp-test text-muted" aria-live="polite"></div>
		<ul class="mcp-checks">
			${checks.map(([key, ok, bad, fix]) => row(c[key] ? "ok" : "todo", c[key] ? ok : bad, c[key] ? "" : mcp_btn("fix:" + key, fix, todo === 1 ? "primary" : ""))).join("")}
			${row("info", `${__s("{0} users have access.", [st.role_users])} <a href="#" data-act="grant">${__s("Give a user access")}</a>`)}
		</ul>
		${todo > 1 ? `<div class="mcp-actions" style="margin-top:12px">${mcp_btn("fix:all", __s("Fix all"), "primary")}</div>` : ""}
	</div>`));
	$w.find(".mcp-url input").on("focus", function () { this.select(); });
	bind_mcp_actions(ui, $w);
}

function draw_mcp_connect(ui, $w) {
	const { url, st } = ui;
	const tool = $w.data("tool") || "claude";
	const link = (href, label) => `<a class="mcp-btn" target="_blank" rel="noopener" href="${href}">${mcp_icon("external")}<span>${label}</span></a>`;
	const copy_link = mcp_btn("copy", __s("Copy link"), "primary", "", "copy");
	const tools = {
		claude: { name: "Claude", steps: [
			__s("Click <b>Open Claude settings</b>, then <b>Add custom connector</b>."),
			__s("Paste the link, click <b>Add</b>, then <b>Connect</b>."),
			__s("Sign in and click <b>Allow</b>. To approve requests later, reply <b>approve</b> in the chat."),
		], buttons: copy_link + link("https://claude.ai/settings/connectors", __s("Open Claude settings")) },
		chatgpt: { name: "ChatGPT", steps: [
			__s("In ChatGPT open <b>Settings → Apps → Advanced settings</b> and turn on <b>Developer mode</b>."),
			__s("Click <b>Create app</b>, paste the link and choose <b>OAuth</b>."),
			__s("Sign in and click <b>Allow</b>."),
		], buttons: copy_link + link("https://chatgpt.com", __s("Open ChatGPT")),
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
		buttons: mcp_btn("copy", __s("Copy link"), "", "", "copy") + link(`/app/user/${encodeURIComponent(frappe.session.user)}`, __s("Open my user page")) },
	};
	const t = tools[tool];
	$w.html(mcp_wrap(`<div class="mcp-card">
		<div class="mcp-tools" role="tablist" aria-label="${__s("Connect an AI Tool")}">${Object.entries(tools).map(([k, v]) =>
			`<button type="button" role="tab" class="mcp-tool" aria-selected="${k === tool}" data-tool="${k}">${frappe.utils.escape_html(v.name)}</button>`).join("")}</div>
		${t.note ? `<div class="mcp-note">${t.note}</div>` : ""}
		<ol class="mcp-steps">${t.steps.map((x) => `<li>${mcp_bidi(x)}</li>`).join("")}</ol>
		${t.snippet ? `<div class="mcp-code"><pre>${frappe.utils.escape_html(t.snippet)}</pre>${mcp_btn("copy-snippet", __s("Copy"), "", "", "copy")}</div>` : ""}
		${t.buttons ? `<div class="mcp-actions">${t.buttons}</div>` : ""}
	</div>`));
	$w.find("[data-tool]").on("click", function () {
		$w.data("tool", $(this).data("tool"));
		draw_mcp_connect(ui, $w);
		$w.find(`[data-tool="${$(this).data("tool")}"]`).trigger("focus");
	});
	$w.find('[data-act="copy-snippet"]').on("click", () => frappe.utils.copy_to_clipboard(t.snippet));
	bind_mcp_actions(ui, $w);
}

function draw_mcp_clients(ui, $w) {
	const esc = frappe.utils.escape_html;
	const apps = ui.st.clients;
	$w.html(mcp_wrap(`<div class="mcp-card">${apps.length
		? `<ul class="mcp-apps">${apps.map((a) => `
			<li class="mcp-app">
				<span class="mcp-app-mark" aria-hidden="true">${esc((a.app_name || "?").trim().charAt(0).toUpperCase())}</span>
				<div class="mcp-app-main"><div class="mcp-app-name">${esc(a.app_name)}</div>
					<div class="mcp-app-hint">${esc(a.users.join(", "))} · ${__s("Last used {0}", [frappe.datetime.comment_when(a.last_used)])}</div></div>
				${mcp_btn("revoke", __s("Disconnect"), "", `data-client="${esc(a.client)}" data-name="${esc(a.app_name)}"`)}
			</li>`).join("")}</ul>`
		: `<div class="mcp-empty">${__s("No app is connected yet.")}</div>`}</div>`));
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
