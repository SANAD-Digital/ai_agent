# Purchase Invoice

Module: Accounts. Submittable: yes.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| supplier | Link | Supplier | Supplier |
| posting_date | Date | Posting Date |  |
| naming_series | Select | Series | ACC-PINV-.YYYY.- |
| items | Table | Items | Purchase Invoice Item |
| credit_to | Link | Credit To | Account |
| company | Link | Company | Company |
| due_date | Date | Due Date |  |
| grand_total | Currency | Grand Total | currency |
| status | Select | Status |  / Draft / Return / Debit Note Issued / Submitted / Paid / P |
| bill_no | Data | Supplier Invoice No |  |
| set_posting_time | Check | Edit Posting Date and Time |  |
| is_paid | Check | Is Paid |  |
| is_return | Check | Is Return (Debit Note) |  |
| return_against | Link | Return Against Purchase Invoice | Purchase Invoice |
| update_outstanding_for_self | Check | Update Outstanding for Self |  |
| update_billed_amount_in_purchase_order | Check | Update Billed Amount in Purchase Order |  |
| update_billed_amount_in_purchase_receipt | Check | Update Billed Amount in Purchase Receipt |  |
| apply_tds | Check | Consider for Tax Withholding |  |
| amended_from | Link | Amended From | Purchase Invoice |
| bill_date | Date | Supplier Invoice Date |  |
| cost_center | Link | Cost Center | Cost Center |
| project | Link | Project | Project |
| currency | Link | Currency | Currency |
| conversion_rate | Float | Exchange Rate |  |
| use_transaction_date_exchange_rate | Check | Use Transaction Date Exchange Rate |  |
| buying_price_list | Link | Price List | Price List |
| price_list_currency | Link | Price List Currency | Currency |
| plc_conversion_rate | Float | Price List Exchange Rate |  |
| ignore_pricing_rule | Check | Ignore Pricing Rule |  |
| update_stock | Check | Update Stock |  |
| set_warehouse | Link | Set Accepted Warehouse | Warehouse |
| set_from_warehouse | Link | Set From Warehouse | Warehouse |
| is_subcontracted | Check | Is Subcontracted |  |
| rejected_warehouse | Link | Rejected Warehouse | Warehouse |
| supplier_warehouse | Link | Supplier Warehouse | Warehouse |
| total_qty | Float | Total Quantity |  |
| total_net_weight | Float | Total Net Weight |  |
| base_total | Currency | Total (Company Currency) | Company:company:default_currency |
| base_net_total | Currency | Net Total (Company Currency) | Company:company:default_currency |
| claimed_landed_cost_amount | Currency | Claimed Landed Cost Amount (Company Currency) | Company:company:default_currency |
| total | Currency | Total | currency |
| net_total | Currency | Net Total | currency |
| tax_category | Link | Tax Category | Tax Category |
| taxes_and_charges | Link | Purchase Taxes and Charges Template | Purchase Taxes and Charges Template |
| shipping_rule | Link | Shipping Rule | Shipping Rule |
