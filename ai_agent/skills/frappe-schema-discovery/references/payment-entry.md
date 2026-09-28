# Payment Entry

Module: Accounts. Submittable: yes.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| payment_type | Select | Payment Type | Receive / Pay / Internal Transfer / Pay Expense / Receive In |
| posting_date | Date | Posting Date |  |
| naming_series | Select | Series | ACC-PAY-.YYYY.- |
| company | Link | Company | Company |
| mode_of_payment | Link | Mode of Payment | Mode of Payment |
| party_type | Link | Party Type | DocType |
| paid_from | Link | Account Paid From | Account |
| paid_to | Link | Account Paid To | Account |
| party | Dynamic Link | Party | party_type |
| reference_no | Data | Cheque/Reference No |  |
| book_advance_payments_in_separate_party_account | Check | Book Advance Payments in Separate Party Account |  |
| apply_tds | Check | Consider for Tax Withholding |  |
| tax_withholding_category | Link | Tax Withholding Category | Tax Withholding Category |
| party_bank_account | Link | Party Bank Account | Bank Account |
| contact_person | Link | Contact | Contact |
| paid_from_account_currency | Link | Account Currency (From) | Currency |
| paid_to_account_currency | Link | Account Currency (To) | Currency |
| paid_amount | Currency | Paid Amount | paid_from_account_currency |
| source_exchange_rate | Float | Source Exchange Rate |  |
| base_paid_amount | Currency | Paid Amount (Company Currency) | Company:company:default_currency |
| received_amount | Currency | Received Amount | paid_to_account_currency |
| target_exchange_rate | Float | Target Exchange Rate |  |
| base_received_amount | Currency | Received Amount (Company Currency) | Company:company:default_currency |
| references | Table | Payment References | Payment Entry Reference |
| total_allocated_amount | Currency | Total Allocated Amount |  |
| base_total_allocated_amount | Currency | Total Allocated Amount (Company Currency) | Company:company:default_currency |
| unallocated_amount | Currency | Unallocated Amount |  |
| difference_amount | Currency | Difference Amount (Company Currency) | Company:company:default_currency |
| purchase_taxes_and_charges_template | Link | Purchase Taxes and Charges Template | Purchase Taxes and Charges Template |
| sales_taxes_and_charges_template | Link | Sales Taxes and Charges Template | Sales Taxes and Charges Template |
| taxes | Table | Advance Taxes and Charges | Advance Taxes and Charges |
| base_total_taxes_and_charges | Currency | Total Taxes and Charges (Company Currency) | Company:company:default_currency |
| total_taxes_and_charges | Currency | Total Taxes and Charges |  |
| deductions | Table | Payment Deductions or Loss | Payment Entry Deduction |
| tax_withholding_group | Link | Tax Withholding Group | Tax Withholding Group |
| ignore_tax_withholding_threshold | Check | Ignore Tax Withholding Threshold |  |
| override_tax_withholding_entries | Check | Edit Tax Withholding Entries |  |
| tax_withholding_entries | Table | Tax Withholding Entries | Tax Withholding Entry |
| reference_date | Date | Cheque/Reference Date |  |
| clearance_date | Date | Clearance Date |  |
| project | Link | Project | Project |
| cost_center | Link | Cost Center | Cost Center |
| status | Select | Status |  / Draft / Submitted / Cancelled |
| is_opening | Select | Is Opening | No / Yes |
| letter_head | Link | Letter Head | Letter Head |
