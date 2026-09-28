"""Default metric catalog, installed on migrate.

Only missing metrics are inserted: once a metric exists, the Accounts Manager
owns its definition and migrations never overwrite it. Every default is
flagged pending_confirmation until the business confirms the definition
(docs/kpi-definitions.md was not provided; see docs/decisions.md).
"""

import json

import frappe

SUBMITTED = ["docstatus", "=", 1]
SALES_DIMS = [
	("customer", "العميل", "customer", "", ""),
	("customer_group", "مجموعة العملاء", "customer_group", "", ""),
	("territory", "المنطقة", "territory", "", ""),
	("branch", "الفرع", "branch", "", ""),
	("cost_center", "مركز التكلفة", "cost_center", "", ""),
	("pos_profile", "نقطة البيع", "pos_profile", "", ""),
	("date", "اليوم", "posting_date", "", ""),
	("item", "الصنف", "items.item_code", "base_net_amount", ""),
	("item_group", "مجموعة الأصناف", "items.item_group", "base_net_amount", ""),
	("sales_person", "المندوب", "sales_team.sales_person", "allocated_amount", ""),
	("payment_mode", "طريقة الدفع", "", "", "payment_mode"),
]
NET_NOTE = "بالعملة الأساسية للشركة، صافي قبل الضريبة (base_net_total)."


def _metric(key, label_ar, label_en, description, doctype, aggregate, value_field, filters, *,
			date_field="posting_date", time_field="", company_field="company", sign=1, dims=(),
			assumption="", currency_note=NET_NOTE, resolver=""):
	return {
		"doctype": "AI Metric",
		"metric_key": key,
		"label_ar": label_ar,
		"label_en": label_en,
		"description": description,
		"enabled": 1,
		"pending_confirmation": 1,
		"source_doctype": doctype,
		"aggregate": aggregate,
		"value_field": value_field,
		"sign": sign,
		"date_field": date_field,
		"time_field": time_field,
		"company_field": company_field,
		"resolver": resolver,
		"base_filters": json.dumps(filters, ensure_ascii=False),
		"currency_note": currency_note,
		"assumption_text": assumption,
		"dimensions": [
			{"dimension_key": k, "label_ar": label, "field_path": path, "value_field": value, "resolver": res}
			for k, label, path, value, res in dims
		],
	}


def default_metrics():
	sales = dict(doctype="Sales Invoice", time_field="posting_time", dims=SALES_DIMS)
	return [
		_metric("net_sales", "صافي المبيعات", "Net Sales", "sales minus returns, submitted invoices",
				aggregate="sum", value_field="base_net_total", filters=[SUBMITTED], **sales,
				assumption="صافي المبيعات = فواتير البيع المعتمدة ناقص المرتجعات، قبل الضريبة."),
		_metric("gross_sales", "إجمالي المبيعات", "Gross Sales", "sales before returns",
				aggregate="sum", value_field="base_net_total", filters=[SUBMITTED, ["is_return", "=", 0]], **sales,
				assumption="إجمالي المبيعات = فواتير البيع المعتمدة بدون المرتجعات، قبل الضريبة."),
		_metric("sales_returns", "مرتجعات المبيعات", "Sales Returns", "returned sales amount (positive)",
				aggregate="sum", value_field="base_net_total", filters=[SUBMITTED, ["is_return", "=", 1]], sign=-1,
				**sales, assumption="المرتجعات = فواتير البيع المرتجعة المعتمدة، بقيمة موجبة، قبل الضريبة."),
		_metric("sales_invoice_count", "عدد فواتير البيع", "Sales Invoice Count", "number of sales invoices",
				aggregate="count", value_field="", filters=[SUBMITTED, ["is_return", "=", 0]],
				doctype="Sales Invoice", time_field="posting_time", dims=[d for d in SALES_DIMS if not d[2].count(".") and not d[4]],
				currency_note="", assumption="عدد فواتير البيع المعتمدة بدون المرتجعات."),
		_metric("avg_invoice_value", "متوسط الفاتورة", "Average Invoice", "average sales invoice value",
				aggregate="avg", value_field="base_net_total", filters=[SUBMITTED, ["is_return", "=", 0]],
				doctype="Sales Invoice", time_field="posting_time", dims=[d for d in SALES_DIMS if not d[2].count(".") and not d[4]],
				assumption="متوسط صافي قيمة فاتورة البيع المعتمدة (بدون المرتجعات)."),
		_metric("sold_qty", "الكمية المباعة", "Sold Quantity", "quantity sold (stock UOM)",
				aggregate="sum", value_field="total_qty", filters=[SUBMITTED, ["is_return", "=", 0]],
				doctype="Sales Invoice", time_field="posting_time",
				dims=[("item", "الصنف", "items.item_code", "stock_qty", ""), ("item_group", "مجموعة الأصناف", "items.item_group", "stock_qty", ""),
					  ("customer", "العميل", "customer", "", "")],
				currency_note="", assumption="الكمية المباعة بوحدة المخزون في فواتير البيع المعتمدة بدون المرتجعات."),
		_metric("net_purchases", "صافي المشتريات", "Net Purchases", "purchases minus returns, submitted invoices",
				doctype="Purchase Invoice", aggregate="sum", value_field="base_net_total", filters=[SUBMITTED],
				dims=[("supplier", "المورد", "supplier", "", ""), ("supplier_group", "مجموعة الموردين", "supplier.supplier_group", "", ""),
					  ("item", "الصنف", "items.item_code", "base_net_amount", ""), ("date", "اليوم", "posting_date", "", "")],
				assumption="صافي المشتريات = فواتير الشراء المعتمدة ناقص المرتجعات، قبل الضريبة."),
		_metric("stock_qty", "رصيد المخزون (كمية)", "Stock Quantity", "current stock quantity",
				doctype="Bin", aggregate="sum", value_field="actual_qty", filters=[], date_field="",
				company_field="warehouse.company",
				dims=[("item", "الصنف", "item_code", "", ""), ("item_name", "اسم الصنف", "item_code.item_name", "", ""),
					  ("item_group", "مجموعة الأصناف", "item_code.item_group", "", ""), ("warehouse", "المخزن", "warehouse", "", "")],
				currency_note="", assumption="الرصيد الفعلي الحالي (actual_qty) بوحدة المخزون، الآن وليس في تاريخ سابق."),
		_metric("stock_value", "قيمة المخزون", "Stock Value", "current stock value",
				doctype="Bin", aggregate="sum", value_field="stock_value", filters=[], date_field="",
				company_field="warehouse.company",
				dims=[("item", "الصنف", "item_code", "", ""), ("item_group", "مجموعة الأصناف", "item_code.item_group", "", ""),
					  ("warehouse", "المخزن", "warehouse", "", "")],
				currency_note="بالعملة الأساسية للشركة.", assumption="قيمة المخزون الحالية حسب التقييم في Bin، الآن وليس في تاريخ سابق."),
		_metric("receivables", "ذمم العملاء", "Receivables", "what customers owe us now (outstanding invoices)",
				doctype="Sales Invoice", aggregate="sum", value_field="outstanding_amount",
				filters=[SUBMITTED, ["outstanding_amount", "!=", 0]], date_field="",
				dims=[("customer", "العميل", "customer", "", ""), ("age", "عمر الدين", "due_date", "", "age_bucket"),
					  ("due_date", "تاريخ الاستحقاق", "due_date", "", "")],
				currency_note="بعملة حساب العميل (غالباً عملة الشركة).",
				assumption="ذمم العملاء = المتبقي على فواتير البيع المعتمدة الآن؛ لا يشمل القيود اليدوية والدفعات غير المخصصة."),
		_metric("payables", "ذمم الموردين", "Payables", "what we owe suppliers now (outstanding invoices)",
				doctype="Purchase Invoice", aggregate="sum", value_field="outstanding_amount",
				filters=[SUBMITTED, ["outstanding_amount", "!=", 0]], date_field="",
				dims=[("supplier", "المورد", "supplier", "", ""), ("age", "عمر الدين", "due_date", "", "age_bucket"),
					  ("due_date", "تاريخ الاستحقاق", "due_date", "", "")],
				currency_note="بعملة حساب المورد (غالباً عملة الشركة).",
				assumption="ذمم الموردين = المتبقي على فواتير الشراء المعتمدة الآن؛ لا يشمل القيود اليدوية والدفعات غير المخصصة."),
		_metric("cash_balance", "النقدية في الصندوق", "Cash Balance", "cash accounts balance at period end",
				doctype="GL Entry", aggregate="balance", value_field="debit - credit",
				filters=[["is_cancelled", "=", 0], ["account.account_type", "=", "Cash"]],
				dims=[("account", "الحساب", "account", "", "")],
				currency_note="بالعملة الأساسية للشركة.", assumption="رصيد حسابات النقدية (نوع Cash) حتى نهاية الفترة."),
		_metric("bank_balance", "أرصدة البنوك", "Bank Balance", "bank accounts balance at period end",
				doctype="GL Entry", aggregate="balance", value_field="debit - credit",
				filters=[["is_cancelled", "=", 0], ["account.account_type", "=", "Bank"]],
				dims=[("account", "الحساب", "account", "", "")],
				currency_note="بالعملة الأساسية للشركة.", assumption="رصيد حسابات البنوك (نوع Bank) حتى نهاية الفترة."),
		_metric("collections", "التحصيلات", "Collections", "money received from customers",
				doctype="Payment Entry", aggregate="sum", value_field="base_received_amount",
				filters=[SUBMITTED, ["payment_type", "=", "Receive"]],
				dims=[("mode_of_payment", "طريقة الدفع", "mode_of_payment", "", ""), ("party", "الطرف", "party", "", ""),
					  ("date", "اليوم", "posting_date", "", "")],
				currency_note="بالعملة الأساسية للشركة.", assumption="سندات القبض المعتمدة (Payment Entry من نوع Receive)."),
		_metric("payments_made", "المدفوعات", "Payments Made", "money paid to suppliers and others",
				doctype="Payment Entry", aggregate="sum", value_field="base_paid_amount",
				filters=[SUBMITTED, ["payment_type", "=", "Pay"]],
				dims=[("mode_of_payment", "طريقة الدفع", "mode_of_payment", "", ""), ("party", "الطرف", "party", "", ""),
					  ("date", "اليوم", "posting_date", "", "")],
				currency_note="بالعملة الأساسية للشركة.", assumption="سندات الصرف المعتمدة (Payment Entry من نوع Pay)."),
		_metric("revenue", "الإيرادات", "Revenue", "income accounts in the period (GL)",
				doctype="GL Entry", aggregate="sum", value_field="credit - debit",
				filters=[["is_cancelled", "=", 0], ["account.root_type", "=", "Income"]],
				dims=[("account", "الحساب", "account", "", ""), ("cost_center", "مركز التكلفة", "cost_center", "", "")],
				currency_note="بالعملة الأساسية للشركة.", assumption="الإيرادات = صافي حركة حسابات الإيرادات (Income) في الفترة من دفتر الأستاذ."),
		_metric("expenses", "المصروفات", "Expenses", "expense accounts in the period (GL), incl. cost of sales",
				doctype="GL Entry", aggregate="sum", value_field="debit - credit",
				filters=[["is_cancelled", "=", 0], ["account.root_type", "=", "Expense"]],
				dims=[("account", "الحساب", "account", "", ""), ("cost_center", "مركز التكلفة", "cost_center", "", "")],
				currency_note="بالعملة الأساسية للشركة.", assumption="المصروفات = صافي حركة حسابات المصروفات (Expense) في الفترة، وتشمل تكلفة المبيعات."),
		_metric("items_below_reorder", "أصناف تحت حد إعادة الطلب", "Items Below Re-order Level",
				"items whose projected qty is below the warehouse re-order level",
				doctype="Bin", aggregate="count", value_field="", filters=[], date_field="", company_field="",
				resolver="below_reorder", currency_note="",
				assumption="مقارنة الكمية المتوقعة (projected_qty) بحد إعادة الطلب لكل مخزن في جدول Item Reorder."),
	]


def default_composites():
	return [
		{
			"doctype": "AI Metric Composite", "composite_key": "net_profit", "label_ar": "صافي الربح",
			"label_en": "Net Profit", "description": "revenue minus expenses (GL)", "enabled": 1,
			"pending_confirmation": 1, "composite_type": "formula", "formula": "revenue - expenses",
			"assumption_text": "صافي الربح = الإيرادات − المصروفات من دفتر الأستاذ للفترة.",
			"items": [{"metric": "revenue", "alias": "revenue"}, {"metric": "expenses", "alias": "expenses"}],
		},
		{
			"doctype": "AI Metric Composite", "composite_key": "sales_return_rate", "label_ar": "نسبة المرتجعات",
			"label_en": "Sales Return Rate", "description": "returns as % of gross sales", "enabled": 1,
			"pending_confirmation": 1, "composite_type": "formula", "formula": "returns / gross * 100",
			"assumption_text": "نسبة المرتجعات = مرتجعات المبيعات ÷ إجمالي المبيعات × 100.",
			"items": [{"metric": "sales_returns", "alias": "returns"}, {"metric": "gross_sales", "alias": "gross"}],
		},
		{
			"doctype": "AI Metric Composite", "composite_key": "company_position", "label_ar": "وضع الشركة",
			"label_en": "Company Position", "description": "fixed bundle: revenue, expenses, cash, banks, receivables, payables, stock value",
			"enabled": 1, "pending_confirmation": 1, "composite_type": "bundle",
			"assumption_text": "الإيرادات والمصروفات للفترة؛ النقدية والبنوك حتى نهاية الفترة؛ الذمم وقيمة المخزون أرصدة حالية.",
			"items": [{"metric": m} for m in (
				"revenue", "expenses", "cash_balance", "bank_balance", "receivables", "payables", "stock_value",
			)],
		},
	]


def ensure_default_metrics():
	"""Insert missing default metrics; never overwrite an existing definition."""
	if not frappe.db.exists("DocType", "AI Metric"):
		return
	for definition in default_metrics():
		if not frappe.db.exists("AI Metric", definition["metric_key"]):
			frappe.get_doc(definition).insert(ignore_permissions=True)
	for definition in default_composites():
		if not frappe.db.exists("AI Metric Composite", definition["composite_key"]):
			frappe.get_doc(definition).insert(ignore_permissions=True)
