# Stock Ledger Entry

Module: Stock. Submittable: no.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| item_code | Link | Item Code | Item |
| warehouse | Link | Warehouse | Warehouse |
| posting_date | Date | Posting Date |  |
| actual_qty | Float | Qty Change |  |
| incoming_rate | Currency | Incoming Rate | Company:company:default_currency |
| voucher_no | Dynamic Link | Voucher No | voucher_type |
| posting_datetime | Datetime | Posting Datetime |  |
| is_adjustment_entry | Check | Is Adjustment Entry |  |
| auto_created_serial_and_batch_bundle | Check | Auto Created Serial and Batch Bundle |  |
| voucher_type | Link | Voucher Type | DocType |
| serial_and_batch_bundle | Link | Serial and Batch Bundle | Serial and Batch Bundle |
| recalculate_rate | Check | Recalculate Incoming/Outgoing Rate |  |
| qty_after_transaction | Float | Qty After Transaction |  |
| outgoing_rate | Currency | Outgoing Rate | Company:company:default_currency |
| valuation_rate | Currency | Average Rate | Company:company:default_currency |
| stock_value | Currency | Balance Stock Value | Company:company:default_currency |
| stock_value_difference | Currency | Change in Stock Value | Company:company:default_currency |
| company | Link | Company | Company |
| stock_uom | Link | Stock UOM | UOM |
| project | Link | Project | Project |
| has_batch_no | Check | Has Batch No |  |
| has_serial_no | Check | Has Serial No |  |
| is_cancelled | Check | Is Cancelled |  |
| posting_time | Time | Posting Time |  |
| voucher_detail_no | Data | Voucher Detail No |  |
| dependant_sle_voucher_detail_no | Data | Dependant SLE Voucher Detail No |  |
| fiscal_year | Data | Fiscal Year |  |
| batch_no | Data | Batch No |  |
| stock_queue | Long Text | FIFO Stock Queue (qty, rate) |  |
| serial_no | Long Text | Serial No |  |
