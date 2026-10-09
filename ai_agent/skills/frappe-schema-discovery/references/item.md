# Item

Module: Stock. Submittable: no.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| item_group | Link | Item Group | Item Group |
| stock_uom | Link | Default Unit of Measure | UOM |
| item_code | Data | Item Code |  |
| has_variants | Check | Has Variants |  |
| is_fixed_asset | Check | Is Fixed Asset |  |
| variant_of | Link | Variant Of | Item |
| is_sales_item | Check | Allow Sales |  |
| disabled | Check | Disabled |  |
| allow_alternative_item | Check | Allow Alternative Item |  |
| is_stock_item | Check | Maintain Stock |  |
| auto_create_assets | Check | Auto create assets on purchase |  |
| is_grouped_asset | Check | Create Grouped Asset |  |
| asset_category | Link | Asset Category | Asset Category |
| asset_naming_series | Select | Asset Naming Series |  |
| standard_rate | Currency | Standard Selling Rate |  |
| over_delivery_receipt_allowance | Float | Over Delivery/Receipt Allowance (%) |  |
| over_billing_allowance | Float | Over Billing Allowance (%) |  |
| brand | Link | Brand | Brand |
| uoms | Table |  | UOM Conversion Detail |
| valuation_method | Select | Valuation Method |  / FIFO / Moving Average / LIFO |
| valuation_rate | Currency | Valuation Rate |  |
| end_of_life | Date | End of Life |  |
| default_material_request_type | Select | Default Material Request Type | Purchase / Material Transfer / Material Issue / Manufacture  |
| weight_per_unit | Float | Weight Per Unit |  |
| weight_uom | Link | Weight UOM | UOM |
| allow_negative_stock | Check | Allow Negative Stock |  |
| barcodes | Table | Barcodes | Item Barcode |
| reorder_levels | Table | Reorder level based on Warehouse | Item Reorder |
| has_batch_no | Check | Has Batch No |  |
| create_new_batch | Check | Automatically Create New Batch |  |
| has_expiry_date | Check | Has Expiry Date |  |
| shelf_life_in_days | Int | Shelf Life In Days |  |
| retain_sample | Check | Retain Sample |  |
| sample_quantity | Int | Max Sample Quantity |  |
| has_serial_no | Check | Has Serial No |  |
| item_defaults | Table | Item Defaults | Item Default |
| variant_based_on | Select | Variant Based On | Item Attribute / Manufacturer |
| enable_deferred_expense | Check | Enable Deferred Expense |  |
| no_of_months_exp | Int | No of Months (Expense) |  |
| enable_deferred_revenue | Check | Enable Deferred Revenue |  |
| no_of_months | Int | No of Months (Revenue) |  |
| purchase_uom | Link | Default Purchase Unit of Measure | UOM |
| min_order_qty | Float | Minimum Order Qty |  |
| safety_stock | Float | Safety Stock |  |
| is_purchase_item | Check | Allow Purchase |  |
