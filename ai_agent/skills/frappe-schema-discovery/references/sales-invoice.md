# Sales Invoice

Module: Accounts. Submittable: yes.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| company | Link | Company | Company |
| customer | Link | Customer | Customer |
| posting_date | Date | Posting Date |  |
| grand_total | Currency | Grand Total | currency |
| naming_series | Select | Series | ACC-SINV-.YYYY.- / ACC-SINV-RET-.YYYY.- |
| currency | Link | Currency | Currency |
| conversion_rate | Float | Exchange Rate |  |
| selling_price_list | Link | Price List | Price List |
| price_list_currency | Link | Price List Currency | Currency |
| plc_conversion_rate | Float | Price List Exchange Rate |  |
| items | Table | Items | Sales Invoice Item |
| base_net_total | Currency | Net Total (Company Currency) | Company:company:default_currency |
| base_grand_total | Currency | Grand Total (Company Currency | Company:company:default_currency |
| debit_to | Link | Debit To | Account |
| due_date | Date | Payment Due Date |  |
| status | Select | Status |  / Draft / Return / Credit Note Issued / Submitted / Paid /  |
| set_posting_time | Check | Edit Posting Date and Time |  |
| is_pos | Check | Include Payment (POS) |  |
| pos_profile | Link | POS Profile | POS Profile |
| is_consolidated | Check | Is Consolidated |  |
| is_return | Check | Is Return (Credit Note) |  |
| return_against | Link | Return Against | Sales Invoice |
| update_outstanding_for_self | Check | Update Outstanding for Self |  |
| update_billed_amount_in_sales_order | Check | Update Billed Amount in Sales Order |  |
| update_billed_amount_in_delivery_note | Check | Update Billed Amount in Delivery Note |  |
| is_debit_note | Check | Is Rate Adjustment Entry (Debit Note) |  |
| apply_tds | Check | Consider for Tax Withholding |  |
| amended_from | Link | Amended From | Sales Invoice |
| cost_center | Link | Cost Center | Cost Center |
| project | Link | Project | Project |
| ignore_pricing_rule | Check | Ignore Pricing Rule |  |
| update_stock | Check | Update Stock |  |
| set_warehouse | Link | Source Warehouse | Warehouse |
| set_target_warehouse | Link | Set Target Warehouse | Warehouse |
| total_qty | Float | Total Quantity |  |
| total_net_weight | Float | Total Net Weight |  |
| base_total | Currency | Total (Company Currency) | Company:company:default_currency |
| total | Currency | Total | currency |
| net_total | Currency | Net Total | currency |
| tax_category | Link | Tax Category | Tax Category |
| taxes_and_charges | Link | Sales Taxes and Charges Template | Sales Taxes and Charges Template |
| shipping_rule | Link | Shipping Rule | Shipping Rule |
| incoterm | Link | Incoterm | Incoterm |
| taxes | Table | Sales Taxes and Charges | Sales Taxes and Charges |
| base_total_taxes_and_charges | Currency | Total Taxes and Charges (Company Currency) | Company:company:default_currency |
