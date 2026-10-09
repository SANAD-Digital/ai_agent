# Journal Entry

Module: Accounts. Submittable: yes.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| company | Link | Company | Company |
| voucher_type | Select | Entry Type | Journal Entry / Inter Company Journal Entry / Bank Entry / C |
| naming_series | Select | Series | ACC-JV-.YYYY.- |
| posting_date | Date | Posting Date |  |
| accounts | Table | Accounting Entries | Journal Entry Account |
| total_debit | Currency | Total Debit | Company:company:default_currency |
| multi_currency | Check | Multi Currency |  |
| apply_tds | Check | Consider for Tax Withholding  |  |
| tax_withholding_category | Link | Tax Withholding Category | Tax Withholding Category |
| is_system_generated | Check | Is System Generated |  |
| amended_from | Link | Amended From | Journal Entry |
| for_all_stock_asset_accounts | Check | For All Stock Asset Accounts |  |
| stock_asset_account | Link | Stock Asset Account | Account |
| periodic_entry_difference_account | Link | Periodic Entry Difference Account | Account |
| total_credit | Currency | Total Credit | Company:company:default_currency |
| difference | Currency | Difference (Dr - Cr) | Company:company:default_currency |
| cheque_date | Date | Reference Date |  |
| clearance_date | Date | Clearance Date |  |
| auto_repeat | Link | Auto Repeat | Auto Repeat |
| tax_withholding_group | Link | Tax Withholding Group | Tax Withholding Group |
| ignore_tax_withholding_threshold | Check | Ignore Tax Withholding Threshold |  |
| override_tax_withholding_entries | Check | Edit Tax Withholding Entries |  |
| tax_withholding_entries | Table | Tax Withholding Entries | Tax Withholding Entry |
| bill_date | Date | Bill Date |  |
| due_date | Date | Due Date |  |
| inter_company_journal_entry_reference | Link | Inter Company Journal Entry Reference | Journal Entry |
| process_deferred_accounting | Link | Process Deferred Accounting | Process Deferred Accounting |
| reversal_of | Link | Reversal Of | Journal Entry |
| payment_order | Link | Payment Order | Payment Order |
| stock_entry | Link | Stock Entry | Stock Entry |
| letter_head | Link | Letter Head | Letter Head |
| select_print_heading | Link | Print Heading | Print Heading |
| write_off_based_on | Select | Write Off Based On | Accounts Receivable / Accounts Payable |
| write_off_amount | Currency | Write Off Amount | Company:company:default_currency |
| is_opening | Select | Is Opening | No / Yes |
| finance_book | Link | Finance Book | Finance Book |
| from_template | Link | From Template | Journal Entry Template |
| custom_remark | Check | Custom Remark |  |
| mode_of_payment | Link | Mode of Payment | Mode of Payment |
| cheque_no | Data | Reference Number |  |
| bill_no | Data | Bill No |  |
| pay_to_recd_from | Data | Pay To / Recd From |  |
| remark | Small Text | Remark |  |
