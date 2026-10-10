// Warn whenever business data can leave to a non-Anthropic provider.
frappe.ui.form.on("AI Settings", {
	refresh(frm) {
		frm.set_intro("");
		if (frm.doc.provider_mode === "Own Key" && frm.doc.allow_third_party_providers) {
			frm.set_intro(
				__("Own Key mode with third-party providers: questions, tool results and attachments are sent to the selected provider (e.g. DeepSeek, Gemini)."),
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
	mcp_agent: render_mcp_guide,
	mcp_enable_action_requests: render_mcp_guide,
});

const MCP_ENDPOINT_PATH = "/api/method/ai_agent.mcp.api.handle";

// Arabic setup guide for every MCP client, built around this site's real endpoint.
function render_mcp_guide(frm) {
	const field = frm.fields_dict.mcp_guide;
	if (!field) return;
	const url = frappe.urllib.get_base_url().replace(/\/$/, "") + MCP_ENDPOINT_PATH;
	const esc = frappe.utils.escape_html;
	const agent = frm.doc.mcp_agent || "ERP Analyst";
	const actions = frm.doc.mcp_enable_action_requests;
	const code = (text) => `<pre class="mcp-code" dir="ltr"><code>${esc(text)}</code></pre>`;
	const steps = (items) => `<ol class="mcp-steps">${items.map((i) => `<li>${i}</li>`).join("")}</ol>`;
	const note = (text, tone = "blue") => `<div class="mcp-note mcp-note-${tone}">${text}</div>`;
	const u = `<code dir="ltr">${esc(url)}</code>`;

	const tabs = [
		{
			id: "claude",
			title: "Claude",
			body:
				`<p>يعمل في <b>claude.ai</b> وتطبيق Claude على سطح المكتب والجوال، لأن الموصلات مرتبطة بحسابك.</p>` +
				steps([
					"افتح <b>Settings ← Connectors</b>.",
					"اضغط <b>Add custom connector</b>.",
					`اكتب اسماً مثل <b>Sanad ERPNext</b>، وضع الرابط: ${u}`,
					"اترك <b>Advanced settings</b> فارغة؛ Claude يسجّل نفسه تلقائياً.",
					"اضغط <b>Add</b> ثم <b>Connect</b>، وسجّل دخولك في النظام، ثم اضغط <b>السماح</b>.",
					"في المحادثة فعّل الموصل من قائمة الأدوات أسفل مربع الكتابة.",
				]) +
				note("في حسابات Team وEnterprise يضيف مالك المؤسسة الموصل أولاً من إعدادات المؤسسة، ثم يضغط كل عضو <b>Connect</b> بحسابه.") +
				note("Claude يعرض النتائج نصاً وجداول في الرد. للموافقة على طلب، اكتب <b>موافق</b> في المحادثة.", "gray"),
		},
		{
			id: "chatgpt",
			title: "ChatGPT",
			body:
				steps([
					"فعّل <b>Developer mode</b> من <b>Settings ← Apps ← Advanced settings</b>.",
					"من <b>Settings ← Apps</b> اضغط <b>Create app</b>.",
					`اكتب الاسم، وضع الرابط ${u}، واختر المصادقة <b>OAuth</b>.`,
					"احفظ، ثم سجّل دخولك في النظام واضغط <b>السماح</b>.",
					"في المحادثة اختر التطبيق من زر <b>+</b> ثم <b>Developer mode</b>.",
				]) +
				note(`يتطلب أن يحتوي <a href="/app/oauth-settings">OAuth Settings</a> في حقل <b>Allowed Public Client Origins</b> على <code dir="ltr">https://chatgpt.com</code>.`, "orange") +
				note("ChatGPT يعرض الجداول والرسوم وبطاقات الموافقة بأزرار داخل المحادثة.", "gray"),
		},
		{
			id: "code",
			title: "Claude Code",
			body:
				`<p>أضف الخادم من الطرفية:</p>` +
				code(`claude mcp add --transport http sanad-erpnext ${url}`) +
				`<p>ثم داخل Claude Code اكتب <code dir="ltr">/mcp</code> واختر الخادم لتسجيل الدخول.</p>`,
		},
		{
			id: "editors",
			title: "Cursor وVS Code وopencode",
			body:
				`<p><b>Cursor</b> — في الملف <code dir="ltr">~/.cursor/mcp.json</code>:</p>` +
				code(JSON.stringify({ mcpServers: { "sanad-erpnext": { url } } }, null, 2)) +
				`<p><b>VS Code (Copilot)</b> — في الملف <code dir="ltr">.vscode/mcp.json</code>:</p>` +
				code(JSON.stringify({ servers: { "sanad-erpnext": { type: "http", url } } }, null, 2)) +
				`<p><b>opencode</b> — في الملف <code dir="ltr">opencode.json</code>:</p>` +
				code(JSON.stringify({ mcp: { "sanad-erpnext": { type: "remote", url, enabled: true } } }, null, 2)) +
				note("هذه الأدوات تفتح صفحة تسجيل الدخول في المتصفح عند أول استخدام. إذا لم تدعم أداةٌ تسجيل الدخول، استخدم طريقة مفتاح API في التبويب التالي."),
		},
		{
			id: "token",
			title: "أدوات أخرى (مفتاح API)",
			body:
				`<p>لأي أداة تدعم MCP عبر HTTP ولا تدعم تسجيل الدخول (OAuth)، استخدم مفتاح API خاصاً بكل مستخدم:</p>` +
				steps([
					"افتح صفحة المستخدم في النظام، ثم <b>Settings ← API Access ← Generate Keys</b>.",
					"انسخ <b>API Key</b> و<b>API Secret</b>؛ السر يظهر مرة واحدة فقط.",
					`في الأداة ضع الرابط ${u} وأضف الترويسة التالية:`,
				]) +
				code("Authorization: token API_KEY:API_SECRET") +
				note("المفتاح يعمل بصلاحيات صاحبه كاملة. لا تستخدم مفتاح مستخدم مدير، ولا تشارك المفتاح بين أشخاص. يمكنك إلغاؤه في أي وقت بإعادة توليده.", "orange"),
		},
		{
			id: "help",
			title: "حل المشاكل",
			body: `<table class="table table-bordered mcp-table"><thead><tr><th>المشكلة</th><th>السبب والحل</th></tr></thead><tbody>
				<tr><td>لا تظهر صفحة تسجيل الدخول</td><td>تأكد أن <a href="/app/oauth-settings">OAuth Settings</a> مفعّل فيه: Show Auth Server Metadata وShow Protected Resource Metadata وEnable Dynamic Client Registration.</td></tr>
				<tr><td>"The authenticated user is not allowed to use MCP"</td><td>أعطِ المستخدم أحد الأدوار في <b>Allowed MCP Roles</b> (افتراضياً <b>AI MCP User</b>).</td></tr>
				<tr><td>"MCP server is disabled"</td><td>فعّل <b>Enable MCP Server</b> في هذه الصفحة واحفظ.</td></tr>
				<tr><td>"MCP request rate limit exceeded"</td><td>زد <b>MCP Requests per Minute</b>، فالحد لكل مستخدم في الدقيقة.</td></tr>
				<tr><td>أدوات جديدة لا تظهر بعد تحديث النظام</td><td>افصل الموصل في الأداة ثم أعد ربطه حتى تجلب قائمة الأدوات من جديد.</td></tr>
				<tr><td>لا يستطيع إنشاء مستندات</td><td>فعّل <b>Enable MCP Action Requests</b>، واختر وكيلاً يسمح بالإجراءات. كل تعديل ينتظر موافقتك.</td></tr>
				</tbody></table>`,
		},
	];

	const status = [
		[frm.doc.enable_mcp, frm.doc.enable_mcp ? "الخادم مفعّل" : "الخادم غير مفعّل"],
		[true, `الوكيل: <b>${esc(agent)}</b>`],
		[true, actions ? "القراءة + طلبات إجراءات بموافقة" : "قراءة فقط"],
	]
		.map(([ok, text]) => `<span class="mcp-chip ${ok ? "ok" : "off"}">${text}</span>`)
		.join("");

	field.$wrapper.html(`
		<style>
			.mcp-guide { direction: rtl; text-align: right; max-width: 900px; }
			.mcp-guide .mcp-url { display: flex; gap: 8px; align-items: center; margin: 4px 0 12px; }
			.mcp-guide .mcp-url code { flex: 1; padding: 8px 10px; border-radius: 8px; background: var(--control-bg); overflow-x: auto; white-space: nowrap; }
			.mcp-guide .mcp-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
			.mcp-guide .mcp-chip { font-size: 12px; padding: 3px 10px; border-radius: 999px; background: var(--bg-green); color: var(--text-on-green); }
			.mcp-guide .mcp-chip.off { background: var(--bg-orange); color: var(--text-on-orange); }
			.mcp-guide .mcp-tabs { display: flex; flex-wrap: wrap; gap: 6px; border-bottom: 1px solid var(--border-color); margin-bottom: 12px; }
			.mcp-guide .mcp-tab { border: 0; background: none; padding: 8px 12px; border-bottom: 2px solid transparent; color: var(--text-muted); font-weight: 500; }
			.mcp-guide .mcp-tab.active { color: var(--text-color); border-bottom-color: var(--primary); }
			.mcp-guide .mcp-panel { display: none; line-height: 1.8; }
			.mcp-guide .mcp-panel.active { display: block; }
			.mcp-guide .mcp-steps { padding-inline-start: 22px; margin-bottom: 10px; }
			.mcp-guide .mcp-steps li { margin-bottom: 4px; }
			.mcp-guide .mcp-code { direction: ltr; text-align: left; background: var(--control-bg); border-radius: 8px; padding: 10px 12px; font-size: 12.5px; white-space: pre-wrap; word-break: break-all; }
			.mcp-guide .mcp-note { padding: 8px 12px; border-radius: 8px; margin: 8px 0; font-size: 13px; }
			.mcp-guide .mcp-note-blue { background: var(--bg-blue); color: var(--text-on-blue); }
			.mcp-guide .mcp-note-orange { background: var(--bg-orange); color: var(--text-on-orange); }
			.mcp-guide .mcp-note-gray { background: var(--control-bg); color: var(--text-muted); }
			.mcp-guide code { direction: ltr; unicode-bidi: isolate; }
			.mcp-guide .mcp-table td, .mcp-guide .mcp-table th { text-align: right; vertical-align: top; }
		</style>
		<div class="mcp-guide">
			<div class="text-muted small">رابط خادم MCP لهذا الموقع:</div>
			<div class="mcp-url"><code dir="ltr">${esc(url)}</code>
				<button class="btn btn-default btn-sm mcp-copy">نسخ</button></div>
			<div class="mcp-chips">${status}</div>
			<p class="text-muted small">كل مستخدم يدخل بحسابه في النظام، فيرى ويعدّل فقط ما تسمح به صلاحياته. يجب أن يملك أحد الأدوار في <b>Allowed MCP Roles</b>.</p>
			<div class="mcp-tabs">${tabs.map((t, i) => `<button class="mcp-tab${i ? "" : " active"}" data-tab="${t.id}">${t.title}</button>`).join("")}</div>
			${tabs.map((t, i) => `<div class="mcp-panel${i ? "" : " active"}" data-panel="${t.id}">${t.body}</div>`).join("")}
		</div>`);

	field.$wrapper.find(".mcp-copy").on("click", () => frappe.utils.copy_to_clipboard(url));
	field.$wrapper.find(".mcp-tab").on("click", function () {
		const id = $(this).data("tab");
		field.$wrapper.find(".mcp-tab").removeClass("active").filter(`[data-tab="${id}"]`).addClass("active");
		field.$wrapper.find(".mcp-panel").removeClass("active").filter(`[data-panel="${id}"]`).addClass("active");
	});
}
