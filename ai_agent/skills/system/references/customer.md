# Customer

Module: Selling. Submittable: no.
Standard ERPNext v16 fields, most used first (site custom fields are not listed).

| fieldname | type | label | options |
|---|---|---|---|
| customer_type | Select | Customer Type | Company / Individual / Partnership |
| customer_name | Data | Customer Name |  |
| customer_group | Link | Customer Group | Customer Group |
| territory | Link | Territory | Territory |
| default_currency | Link | Billing Currency | Currency |
| gender | Link | Gender | Gender |
| default_bank_account | Link | Company Bank Account | Bank Account |
| default_price_list | Link | Price List | Price List |
| customer_primary_address | Link | Customer Primary Address | Address |
| customer_primary_contact | Link | Customer Primary Contact | Contact |
| tax_category | Link | Tax Category | Tax Category |
| tax_withholding_category | Link | Tax Withholding Category | Tax Withholding Category |
| tax_withholding_group | Link | Tax Withholding Group | Tax Withholding Group |
| accounts | Table | Default Accounts | Party Account |
| payment_terms | Link | Payment Terms Template | Payment Terms Template |
| credit_limits | Table | Credit Limit | Customer Credit Limit |
| is_internal_customer | Check | Is Internal Customer |  |
| represents_company | Link | Represents Company | Company |
| companies | Table | Allowed to transact with | Allowed To Transact With |
| loyalty_program | Link | Loyalty Program | Loyalty Program |
| account_manager | Link | Account Manager | User |
| sales_team | Table | Sales Team | Sales Team |
| default_sales_partner | Link | Sales Partner | Sales Partner |
| default_commission_rate | Float | Commission Rate |  |
| so_required | Check | Allow sales invoice creation without sales order |  |
| dn_required | Check | Allow sales invoice creation without delivery note |  |
| disabled | Check | Disabled |  |
| is_frozen | Check | Is Frozen |  |
| portal_users | Table | Customer Portal Users | Portal User |
| lead_name | Link | Lead | Lead |
| opportunity_name | Link | Opportunity | Opportunity |
| prospect_name | Link | Prospect | Prospect |
| market_segment | Link | Market Segment | Market Segment |
| industry | Link | Industry | Industry Type |
| language | Link | Print Language | Language |
| supplier_numbers | Table | Supplier Numbers | Supplier Number At Customer |
| alias | Data | Alias |  |
| mobile_no | Read Only | Mobile No | Mobile |
| email_id | Read Only | Email Id | Email |
| tax_id | Data | Tax ID |  |
| loyalty_program_tier | Data | Loyalty Program Tier |  |
| website | Data | Website |  |
| customer_pos_id | Data | Customer POS ID |  |
| primary_address | Text Editor | Primary Address |  |
| customer_details | Text | Customer Details |  |
