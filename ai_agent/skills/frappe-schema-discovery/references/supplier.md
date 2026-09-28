# Supplier

Module: Buying. Submittable: no.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| supplier_type | Select | Supplier Type | Company / Individual / Partnership |
| supplier_name | Data | Supplier Name |  |
| supplier_group | Link | Supplier Group | Supplier Group |
| default_currency | Link | Billing Currency | Currency |
| gender | Link | Gender | Gender |
| country | Link | Country | Country |
| default_bank_account | Link | Company Bank Account | Bank Account |
| default_price_list | Link | Price List | Price List |
| payment_terms | Link | Payment Terms Template | Payment Terms Template |
| supplier_primary_address | Link | Primary Address | Address |
| supplier_primary_contact | Link | Primary Contact | Contact |
| accounts | Table | Per-Company Accounts | Party Account |
| is_internal_supplier | Check | Is Internal Supplier |  |
| represents_company | Link | Represents Company | Company |
| companies | Table | Allowed to transact with | Allowed To Transact With |
| tax_category | Link | Tax Category | Tax Category |
| tax_withholding_category | Link | Tax Withholding Category | Tax Withholding Category |
| tax_withholding_group | Link | Tax Withholding Group | Tax Withholding Group |
| is_transporter | Check | Is Transporter |  |
| allow_purchase_invoice_creation_without_purchase_order | Check | Allow purchase invoice creation without purchase order |  |
| allow_purchase_invoice_creation_without_purchase_receipt | Check | Allow purchase invoice creation without purchase receipt |  |
| disabled | Check | Disabled |  |
| is_frozen | Check | Is Frozen |  |
| on_hold | Check | Block Supplier |  |
| hold_type | Select | Hold Type | All / Invoices / Payments |
| release_date | Date | Release Date |  |
| portal_users | Table | Supplier Portal Users | Portal User |
| language | Link | Print Language | Language |
| customer_numbers | Table | Customer Numbers | Customer Number At Supplier |
| alias | Data | Alias |  |
| mobile_no | Read Only | Mobile No |  |
| email_id | Read Only | Email ID |  |
| tax_id | Data | Tax ID |  |
| website | Data | Website |  |
| primary_address | Text Editor | Primary Address Preview |  |
| supplier_details | Text | Supplier Details |  |
