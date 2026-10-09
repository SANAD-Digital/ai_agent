# Journal Entry Account

Module: Accounts. Submittable: no. Child table.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| account | Link | Account | Account |
| party_type | Link | Party Type | DocType |
| debit_in_account_currency | Currency | Debit | account_currency |
| credit_in_account_currency | Currency | Credit | account_currency |
| party | Dynamic Link | Party | party_type |
| bank_account | Link | Bank Account | Bank Account |
| cost_center | Link | Cost Center | Cost Center |
| project | Link | Project | Project |
| account_currency | Link | Account Currency | Currency |
| exchange_rate | Float | Exchange Rate |  |
| debit | Currency | Debit in Company Currency | Company:company:default_currency |
| credit | Currency | Credit in Company Currency | Company:company:default_currency |
| reference_type | Select | Reference Type |  / Sales Invoice / Purchase Invoice / Journal Entry / Sales  |
| reference_due_date | Date | Reference Due Date |  |
| advance_voucher_type | Link | Advance Voucher Type | DocType |
| is_tax_withholding_account | Check | Is Tax Withholding Account |  |
| is_advance | Select | Is Advance | No / Yes |
| reference_name | Dynamic Link | Reference Name | reference_type |
| advance_voucher_no | Dynamic Link | Advance Voucher No | advance_voucher_type |
| user_remark | Small Text | User Remark |  |
