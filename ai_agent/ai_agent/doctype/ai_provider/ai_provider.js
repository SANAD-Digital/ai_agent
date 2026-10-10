// A non-Anthropic provider sends business data to a third party.
frappe.ui.form.on("AI Provider", {
	refresh(frm) {
		frm.set_intro("");
		if (frm.doc.provider_type && frm.doc.provider_type !== "Anthropic") {
			frm.set_intro(
				__("This provider is outside the managed (Anthropic) setup. It works only in Own Key mode with third-party consent in AI Settings, and data sent to it leaves to {0}.", [frm.doc.provider_type]),
				"orange"
			);
		}
		if (!frm.is_new()) {
			frm.add_custom_button(__("جلب النماذج من المزوّد"), () => fetch_models(frm));
		}
		// the chat lists a provider only once it has models (chat/api.py available_providers)
		if (frm.doc.enabled && !(frm.doc.models || []).length && !frm.doc.default_model) {
			frm.dashboard.set_headline_alert(
				__("هذا المزوّد لن يظهر في المحادثة بعد: لا نماذج فيه. اضغط «جلب النماذج من المزوّد» واختر النماذج ثم «حفظ المختار»."),
				"orange"
			);
		}
	},
	provider_type(frm) {
		frm.trigger("refresh");
	},
});

// Ask the provider which models this account can use, and add the picked ones
// with the capabilities the provider reports (no manual lookup on its website).
function fetch_models(frm) {
	// the fetch uses the saved key and URL: save pending edits first
	const ready = frm.is_dirty() ? frm.save() : Promise.resolve();
	ready
		.then(() =>
			frappe.call({
				method: "ai_agent.ai_agent.doctype.ai_provider.ai_provider.fetch_models",
				args: { provider: frm.doc.name },
				freeze: true,
				freeze_message: __("جارٍ سؤال المزوّد عن نماذجه…"),
			})
		)
		.then((r) => r && r.message && show_models_dialog(frm, r.message.models || [], r.message.stale || []));
}

function show_models_dialog(frm, models, stale) {
	if (!models.length) {
		frappe.msgprint(__("لم يُرجع المزوّد أي نموذج محادثة لهذا الحساب."));
		return;
	}
	const esc = frappe.utils.escape_html;
	const num = (n) => (n ? Number(n).toLocaleString() : "—");
	const yes = (v) => (v ? "✓" : "");
	const rows = models
		.map(
			(m, i) => `<tr class="ai-model-row" data-search="${esc((m.model_name + " " + (m.label || "")).toLowerCase())}">
				<td><input type="checkbox" class="ai-pick" data-i="${i}" ${m.exists ? "checked" : ""}></td>
				<td><code>${esc(m.model_name)}</code><div class="text-muted small">${esc(m.label || "")}</div></td>
				<td class="text-right">${num(m.context_window_tokens)}</td>
				<td class="text-right">${num(m.max_output_tokens)}</td>
				<td class="text-center">${yes(m.supports_vision)}</td>
				<td class="text-center">${yes(m.supports_thinking)}</td>
				<td class="text-center">${m.supports_tools ? "✓" : `<span class="text-danger" title="${__("الوكيل يحتاج الأدوات ليقرأ بيانات النظام")}">✗</span>`}</td>
				<td>${m.exists ? `<span class="indicator-pill green">${__("مضاف")}</span>` : `<span class="indicator-pill blue">${__("جديد")}</span>`}</td>
			</tr>`
		)
		.join("");
	const staleNote = stale.length
		? `<div class="alert alert-warning small">${__("هذه النماذج مضبوطة عندك لكن المزوّد لم يعد يعرضها، وقد تتوقف: {0}", [stale.map((s) => `<code>${esc(s)}</code>`).join("، ")])}</div>`
		: "";
	const d = new frappe.ui.Dialog({
		title: __("نماذج {0}", [frm.doc.provider_name || frm.doc.name]),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "list",
				options: `${staleNote}
					<div class="d-flex align-items-center mb-2" style="gap:12px">
						<input type="search" class="form-control ai-model-filter" placeholder="${__("ابحث في {0} نموذج… (مثلاً claude، llama، free)", [models.length])}">
						<span class="text-muted small ai-model-count text-nowrap"></span>
					</div>
					<div style="max-height:55vh;overflow:auto"><table class="table table-bordered table-sm">
					<thead><tr><th></th><th>${__("النموذج")}</th><th>${__("نافذة السياق")}</th><th>${__("أقصى إخراج")}</th><th>${__("صور")}</th><th>${__("تفكير")}</th><th>${__("أدوات")}</th><th></th></tr></thead>
					<tbody>${rows}</tbody></table></div>`,
			},
			{
				fieldtype: "Select",
				fieldname: "default_model",
				label: __("النموذج الافتراضي"),
				options: [""].concat(models.map((m) => m.model_name)).join("\n"),
				default: models.some((m) => m.model_name === frm.doc.default_model) ? frm.doc.default_model : "",
				description: __("يُستخدم حين لا يختار المستخدم نموذجاً."),
			},
			{
				fieldtype: "Check",
				fieldname: "drop_stale",
				label: __("احذف النماذج التي لم يعد المزوّد يعرضها"),
				hidden: !stale.length,
			},
		],
		primary_action_label: __("حفظ المختار"),
		primary_action(values) {
			const picked = Array.from(d.$wrapper.find(".ai-pick:checked")).map((el) => models[Number(el.dataset.i)]);
			if (!picked.length) {
				frappe.msgprint(__("اختر نموذجاً واحداً على الأقل."));
				return;
			}
			apply_models(frm, picked, values.default_model, values.drop_stale ? stale : []);
			d.hide();
			frm.save().then(() => frappe.show_alert({ message: __("حُدّثت النماذج: {0}", [picked.length]), indicator: "green" }));
		},
	});
	const $filter = d.$wrapper.find(".ai-model-filter");
	const paintCount = () => d.$wrapper.find(".ai-model-count").text(__("المختار: {0}", [d.$wrapper.find(".ai-pick:checked").length]));
	$filter.on("input", () => {
		const q = String($filter.val() || "").trim().toLowerCase();
		d.$wrapper.find(".ai-model-row").each((_, tr) => {
			tr.hidden = q && !tr.dataset.search.includes(q);
		});
	});
	d.$wrapper.on("change", ".ai-pick", paintCount);
	paintCount();
	d.show();
}

const MODEL_FIELDS = ["label", "context_window_tokens", "max_output_tokens", "supports_vision", "supports_thinking", "supports_pdf", "supports_tools"];

function apply_models(frm, picked, default_model, drop) {
	if (drop.length) {
		frm.doc.models = (frm.doc.models || []).filter((row) => !drop.includes(row.model_name));
		if (drop.includes(frm.doc.default_model)) frm.doc.default_model = "";
	}
	for (const m of picked) {
		let row = (frm.doc.models || []).find((r) => r.model_name === m.model_name);
		if (!row) row = frm.add_child("models", { model_name: m.model_name });
		for (const field of MODEL_FIELDS) {
			// keep what the admin set by hand when the provider reports nothing for it
			if (m[field] || !row[field]) row[field] = m[field];
		}
	}
	if (default_model) {
		(frm.doc.models || []).forEach((row) => (row.is_default = row.model_name === default_model ? 1 : 0));
		frm.doc.default_model = default_model;
	}
	(frm.doc.models || []).forEach((row, i) => (row.idx = i + 1));
	frm.refresh_field("models");
	frm.dirty();
}

// Frappe checks the strength of every Password field as you type; for a long API key the
// check fails on the server ("Integer exceeds 64-bit range") and the key ends up in Error Log.
frappe.ui.form.on("AI Provider", {
	refresh(frm) {
		frm.get_field("api_key")?.disable_password_checks?.();
	},
});
