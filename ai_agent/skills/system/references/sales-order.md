# Sales Order

Module: Selling. Submittable: yes.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| company | Link | Company | Company |
| customer | Link | Customer | Customer |
| transaction_date | Date | Date |  |
| status | Select | Status |  / Draft / On Hold / To Pay / To Deliver and Bill / To Bill  |
| naming_series | Select | Series | SAL-ORD-.YYYY.- |
| order_type | Select | Order Type |  / Sales / Maintenance / Shopping Cart |
| currency | Link | Currency | Currency |
| conversion_rate | Float | Exchange Rate |  |
| selling_price_list | Link | Price List | Price List |
| price_list_currency | Link | Price List Currency | Currency |
| plc_conversion_rate | Float | Price List Exchange Rate |  |
| items | Table | Items | Sales Order Item |
| delivery_date | Date | Delivery Date |  |
| grand_total | Currency | Grand Total | currency |
| per_delivered | Percent | %  Delivered |  |
| per_billed | Percent | % Amount Billed |  |
| skip_delivery_note | Check | Skip Delivery Note |  |
| is_subcontracted | Check | Is Subcontracted |  |
| cost_center | Link | Cost Center | Cost Center |
| project | Link | Project | Project |
| ignore_pricing_rule | Check | Ignore Pricing Rule |  |
| set_warehouse | Link | Set Source Warehouse | Warehouse |
| reserve_stock | Check | Reserve Stock |  |
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
| advance_paid | Currency | Advance Paid | party_account_currency |
| apply_discount_on | Select | Apply Additional Discount On |  / Grand Total / Net Total |
| base_discount_amount | Currency | Additional Discount Amount | Company:company:default_currency |
