// This form's strings are translated in its own context, so its wording does not change
// the same words elsewhere in Desk (Frappe falls back to the plain translation).
const __g = (text, args) => __(text, args, "Gemini OCR Settings");

frappe.ui.form.on("Gemini OCR Settings", {
    refresh(frm) {
        frm.add_custom_button(__g("Test Connection"), () => {
            frappe.call({
                method: "ai_agent.ai_agent.doctype.gemini_ocr_settings.gemini_ocr_settings.test_connection",
                freeze: true,
                freeze_message: __g("Testing Gemini OCR..."),
                callback(response) {
                    const data = response.message || {};
                    frappe.msgprint({
                        title: __g("Gemini OCR"),
                        indicator: data.ok ? "green" : "red",
                        message: data.message || __g("No response."),
                    });
                },
                error(error) {
                    console.error("Gemini OCR test failed:", error);
                    frappe.msgprint({
                        title: __g("Gemini OCR"),
                        indicator: "red",
                        message: __g("Gemini OCR test failed. Check the Error Log for details."),
                    });
                },
            });
        });
    },
});

// Frappe checks the strength of every Password field as you type; for a long API key the
// check fails on the server ("Integer exceeds 64-bit range") and the key ends up in Error Log.
frappe.ui.form.on("Gemini OCR Settings", {
	refresh(frm) {
		frm.get_field("api_key")?.disable_password_checks?.();
	},
});
