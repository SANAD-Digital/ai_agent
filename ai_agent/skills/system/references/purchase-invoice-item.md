# Purchase Invoice Item

Module: Accounts. Submittable: no. Child table.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| qty | Float | Accepted Qty |  |
| rate | Currency | Rate | currency |
| amount | Currency | Amount | currency |
| uom | Link | UOM | UOM |
| conversion_factor | Float | UOM Conversion Factor |  |
| stock_qty | Float | Accepted Qty in Stock UOM |  |
| base_rate | Currency | Rate (Company Currency) | Company:company:default_currency |
| base_amount | Currency | Amount (Company Currency) | Company:company:default_currency |
| item_name | Data | Item Name |  |
| item_code | Link | Item | Item |
| product_bundle | Link | Product Bundle | Product Bundle |
| item_group | Link | Item Group | Item Group |
| received_qty | Float | Received Qty |  |
| rejected_qty | Float | Rejected Qty |  |
| stock_uom | Link | Stock UOM | UOM |
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
| allow_zero_valuation_rate | Check | Allow Zero Valuation Rate |  |
| net_rate | Currency | Net Rate | currency |
| net_amount | Currency | Net Amount | currency |
| base_net_rate | Currency | Net Rate (Company Currency) | Company:company:default_currency |
| base_net_amount | Currency | Net Amount (Company Currency) | Company:company:default_currency |
| landed_cost_voucher_amount | Currency | Landed Cost Voucher Amount | Company:company:default_currency |
| warehouse | Link | Accepted Warehouse | Warehouse |
| serial_and_batch_bundle | Link | Serial and Batch Bundle | Serial and Batch Bundle |
| use_serial_batch_fields | Check | Use Serial No / Batch Fields |  |
| quality_inspection | Link | Quality Inspection | Quality Inspection |
| rejected_warehouse | Link | Rejected Warehouse | Warehouse |
| rejected_serial_and_batch_bundle | Link | Rejected Serial and Batch Bundle | Serial and Batch Bundle |
| batch_no | Link | Batch No | Batch |
| manufacturer | Link | Manufacturer | Manufacturer |
| expense_account | Link | Expense Head | Account |
| wip_composite_asset | Link | WIP Composite Asset | Asset |
| asset_location | Link | Asset Location | Location |
