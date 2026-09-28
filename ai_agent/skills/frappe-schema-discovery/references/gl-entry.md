# GL Entry

Module: Accounts. Submittable: no.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| posting_date | Date | Posting Date |  |
| transaction_date | Date | Transaction Date |  |
| account | Link | Account | Account |
| cost_center | Link | Cost Center | Cost Center |
| party | Dynamic Link | Party | party_type |
| voucher_no | Dynamic Link | Voucher No | voucher_type |
| fiscal_year | Link | Fiscal Year | Fiscal Year |
| due_date | Date | Due Date |  |
| account_currency | Link | Account Currency | Currency |
| party_type | Link | Party Type | DocType |
| voucher_type | Link | Voucher Type | DocType |
| transaction_currency | Link | Transaction Currency | Currency |
| against_voucher_type | Link | Against Voucher Type | DocType |
| transaction_exchange_rate | Float | Transaction Exchange Rate |  |
| reporting_currency_exchange_rate | Float | Reporting Currency Exchange Rate |  |
| debit_in_account_currency | Currency | Debit Amount in Account Currency | account_currency |
| debit | Currency | Debit Amount | Company:company:default_currency |
| debit_in_transaction_currency | Currency | Debit Amount in Transaction Currency | transaction_currency |
| debit_in_reporting_currency | Currency | Debit Amount in Reporting Currency | Company:company:reporting_currency |
| credit_in_account_currency | Currency | Credit Amount in Account Currency | account_currency |
| credit | Currency | Credit Amount | Company:company:default_currency |
| credit_in_transaction_currency | Currency | Credit Amount in Transaction Currency | transaction_currency |
| credit_in_reporting_currency | Currency | Credit Amount in Reporting Currency | Company:company:reporting_currency |
| project | Link | Project | Project |
| finance_book | Link | Finance Book | Finance Book |
| company | Link | Company | Company |
| is_opening | Select | Is Opening | No / Yes |
| is_advance | Select | Is Advance | No / Yes |
| is_cancelled | Check | Is Cancelled |  |
| against_voucher | Dynamic Link | Against Voucher | against_voucher_type |
| voucher_detail_no | Data | Voucher Detail No |  |
| against | Text | Against |  |
| voucher_subtype | Small Text | Voucher Subtype |  |
| remarks | Text | Remarks |  |
