---
name: setup-readiness
description: Check whether ERPNext is set up for an area (company, accounts, stock, selling, buying) and explain what is missing.
tools: setup.check, knowledge.search
---

# Setup readiness

Checklist per area (setup.check reads the live values; this is what it compares against):

- company: default currency, country, default cost center, a fiscal year covering today, at least one warehouse.
- accounts: default receivable, payable, income, expense, round-off, exchange gain/loss and cash accounts on the company; every Mode of Payment linked to an account for the company; frozen-accounts date noted.
- stock: valuation method and default UOM in Stock Settings; with perpetual inventory, the inventory, stock adjustment and stock-received-not-billed accounts on the company; negative stock flagged; at least one stock item.
- selling: default customer group, territory and selling price list in Selling Settings; an enabled selling price list.
- buying: default supplier group and buying price list in Buying Settings; at least one enabled supplier.

1. Call setup.check for each area the user asks about (all five for "is the system ready").
2. Report only what is missing or risky, grouped by area, each with its steps. Mention "unknown" items as permission limits.
3. Read only: never change settings. Settings are changed by an administrator in the desk.
4. Use knowledge.search when the user guide has more detailed steps, and cite it.
