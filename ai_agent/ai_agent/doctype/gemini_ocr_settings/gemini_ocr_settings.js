frappe.ui.form.on("Gemini OCR Settings", {
    refresh(frm) {
        frm.add_custom_button(__("Test Connection"), () => {
            frappe.call({
                method: "ai_agent.ai_agent.doctype.gemini_ocr_settings.gemini_ocr_settings.test_connection",
                freeze: true,
                freeze_message: __("Testing Gemini OCR..."),
                callback(response) {
                    const data = response.message || {};
                    frappe.msgprint({
                        title: __("Gemini OCR"),
                        indicator: data.ok ? "green" : "red",
                        message: data.message || __("No response."),
                    });
                },
                error(error) {
                    console.error("Gemini OCR test failed:", error);
                    frappe.msgprint({
                        title: __("Gemini OCR"),
                        indicator: "red",
                        message: __("Gemini OCR test failed. Check the Error Log for details."),
                    });
                },
            });
        });
    },
});
