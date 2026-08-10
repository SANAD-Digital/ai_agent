frappe.ui.form.on("Financial Attachment Session", {
    refresh(frm) {
        if (!frm.is_new()) {
            frm.add_custom_button(__("فتح في مساحة العمل"), function () {
                frappe.set_route("purchase-invoice-ai", { session: frm.doc.name });
            }).addClass("btn-primary");

            frm.page.set_primary_action(__("فتح في مساحة العمل"), function () {
                frappe.set_route("purchase-invoice-ai", { session: frm.doc.name });
            }, "fa fa-external-link");
        }
    }
});
