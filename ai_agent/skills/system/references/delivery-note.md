# Delivery Note

Module: Stock. Submittable: yes.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| customer | Link | Customer | Customer |
| posting_date | Date | Date |  |
| company | Link | Company | Company |
| status | Select | Status |  / Draft / To Bill / Partially Billed / Completed / Return / |
| naming_series | Select | Series | MAT-DN-.YYYY.- / MAT-DN-RET-.YYYY.- |
| currency | Link | Currency | Currency |
| conversion_rate | Float | Exchange Rate |  |
| selling_price_list | Link | Price List | Price List |
| price_list_currency | Link | Price List Currency | Currency |
| plc_conversion_rate | Float | Price List Exchange Rate |  |
| items | Table | Delivery Note Item | Delivery Note Item |
| posting_time | Time | Posting Time |  |
| set_target_warehouse | Link | Set Target Warehouse | Warehouse |
| grand_total | Currency | Grand Total | currency |
| per_billed | Percent | % Amount Billed |  |
| per_returned | Percent | % Returned |  |
| set_posting_time | Check | Edit Posting Date and Time |  |
| amended_from | Link | Amended From | Delivery Note |
| is_return | Check | Is Return |  |
| issue_credit_note | Check | Issue Credit Note |  |
| return_against | Link | Return Against Delivery Note | Delivery Note |
| cost_center | Link | Cost Center | Cost Center |
| project | Link | Project | Project |
| ignore_pricing_rule | Check | Ignore Pricing Rule |  |
| set_warehouse | Link | Set Source Warehouse | Warehouse |
| total_qty | Float | Total Quantity |  |
| total_net_weight | Float | Total Net Weight |  |
| base_total | Currency | Total (Company Currency) | Company:company:default_currency |
| base_net_total | Currency | Net Total (Company Currency) | Company:company:default_currency |
| total | Currency | Total | currency |
| net_total | Currency | Net Total | currency |
| tax_category | Link | Tax Category | Tax Category |
| taxes_and_charges | Link | Sales Taxes and Charges Template | Sales Taxes and Charges Template |
| shipping_rule | Link | Shipping Rule | Shipping Rule |
| incoterm | Link | Incoterm | Incoterm |
| taxes | Table | Sales Taxes and Charges | Sales Taxes and Charges |
| base_total_taxes_and_charges | Currency | Total Taxes and Charges (Company Currency) | Company:company:default_currency |
| total_taxes_and_charges | Currency | Total Taxes and Charges | currency |
| disable_rounded_total | Check | Disable Rounded Total |  |
| rounding_adjustment | Currency | Rounding Adjustment | currency |
| rounded_total | Currency | Rounded Total | currency |
| base_grand_total | Currency | Grand Total | Company:company:default_currency |
| base_rounding_adjustment | Currency | Rounding Adjustment | Company:company:default_currency |
| base_rounded_total | Currency | Rounded Total | Company:company:default_currency |
| apply_discount_on | Select | Apply Additional Discount On |  / Grand Total / Net Total |
