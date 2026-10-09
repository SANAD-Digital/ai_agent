---
name: diagnose-item-search
description: Why an item does not appear when searching in sales or purchase documents.
tools: diagnose.document, data.get_document, data.query, knowledge.search
---

# Diagnose: item not found in search

1. If the user gave a name rather than a code, find the item with data.query on Item (item_name like), then call diagnose.document with doctype Item and the company.
2. Causes: disabled; end of life passed; template item with variants; not a sales item (sales documents) or not a purchase item (purchase documents); missing Item Default for the company.
3. Explain the fix as steps in the Item form.
