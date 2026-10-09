# Sales Invoice Item

Module: Accounts. Submittable: no. Child table.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| rate | Currency | Rate | currency |
| amount | Currency | Amount | currency |
| uom | Link | UOM | UOM |
| conversion_factor | Float | UOM Conversion Factor |  |
| base_rate | Currency | Rate (Company Currency) | Company:company:default_currency |
| base_amount | Currency | Amount (Company Currency) | Company:company:default_currency |
| income_account | Link | Income Account | Account |
| cost_center | Link | Cost Center | Cost Center |
| item_name | Data | Item Name |  |
| item_code | Link | Item | Item |
| qty | Float | Quantity |  |
| warehouse | Link | Warehouse | Warehouse |
| has_item_scanned | Check | Has Item Scanned |  |
| stock_uom | Link | Stock UOM | UOM |
| stock_qty | Float | Qty as per Stock UOM |  |
| price_list_rate | Currency | Price List Rate | currency |
| base_price_list_rate | Currency | Price List Rate (Company Currency) | Company:company:default_currency |
| margin_type | Select | Margin Type |  / Percentage / Amount |
| margin_rate_or_amount | Float | Margin Rate or Amount |  |
| rate_with_margin | Currency | Rate With Margin | currency |
| discount_amount | Currency | Discount Amount | currency |
| distributed_discount_amount | Currency | Distributed Discount Amount | currency |
| base_rate_with_margin | Currency | Rate With Margin (Company Currency) | Company:company:default_currency |
| item_tax_template | Link | Item Tax Template | Item Tax Template |
| tax_withholding_category | Link | Tax Withholding Category | Tax Withholding Category |
| stock_uom_rate | Currency | Rate of Stock UOM | currency |
| is_free_item | Check | Is Free Item |  |
| apply_tds | Check | Consider for Tax Withholding |  |
| grant_commission | Check | Grant Commission |  |
| allow_zero_valuation_rate | Check | Allow Zero Valuation Rate |  |
| net_rate | Currency | Net Rate | currency |
| net_amount | Currency | Net Amount | currency |
| base_net_rate | Currency | Net Rate (Company Currency) | Company:company:default_currency |
| base_net_amount | Currency | Net Amount (Company Currency) | Company:company:default_currency |
| delivered_by_supplier | Check | Delivered By Supplier |  |
| asset | Link | Asset | Asset |
| finance_book | Link | Finance Book | Finance Book |
| expense_account | Link | Expense Account | Account |
| deferred_revenue_account | Link | Deferred Revenue Account | Account |
| service_stop_date | Date | Service Stop Date |  |
| enable_deferred_revenue | Check | Enable Deferred Revenue |  |
| service_start_date | Date | Service Start Date |  |
| service_end_date | Date | Service End Date |  |
| weight_per_unit | Float | Weight Per Unit |  |
| total_weight | Float | Total Weight |  |
