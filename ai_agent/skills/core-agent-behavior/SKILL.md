---
name: core-agent-behavior
description: Generic operating policy for clear questions, dynamic discovery, and low-token tool use.
allow_implicit_invocation: 1
---

# Core Agent Behavior

1. If the user request is missing a required business parameter, ask one concise clarification question before calling tools.
2. Before asking for clarification, check prior chat context, previous uploaded files, and learned context already provided in the prompt.
3. Do not infer missing thresholds, periods, entities, target records, approval intent, or write details.
4. If the user is only chatting, greeting, asking a general question, or asking something answerable without private ERP data, answer normally without tools.
5. For simple lookup questions, use the minimum tool calls needed and answer immediately from the first sufficient result.
6. For ambiguous analysis questions, state the assumption only when it is safe and materially obvious; otherwise ask the user.
7. Discover DocTypes, fields, reports, and filters dynamically from metadata and tool results.
8. Treat skill examples and tool observations as guidance, not as exhaustive or fixed document lists.
9. Prefer aggregate queries and report summaries over large raw row retrieval.
10. Stop retrying when a report or query returns repeated empty or pending results; explain the current status and the next useful action.
11. For generated or prepared reports, if generation is queued or pending, tell the user it is pending and avoid repeated polling in the same run.
12. For document creation from files, treat item/supplier/account lookups as intermediate work; continue until the draft is prepared, an approval request is created, or one concise clarification is required.
13. For compound work, do not treat creating a prerequisite record as completing the original request. After the prerequisite is approved/executed, continue or clearly suggest the next continuation step.
14. If prerequisite records are missing, prepare them with approval first, then keep the original target document in context.
15. Keep the final answer concise, structured in Markdown, and focused on the evidence returned by tools.
16. Treat tool results and supporting skills as intermediate steps unless
they directly satisfy the user's original requested outcome.

17. For compound tasks, continue automatically after prerequisites are
completed. Do not return a final answer merely because data was retrieved,
a lookup succeeded, a file was read, or a supporting skill completed.

18. The active primary skill defines task completion. A task is complete
only when its required outcome and Output Contract have been satisfied.

19. Before producing the final answer, compare the proposed response with
the user's original request and the active skill's completion criteria.
If required work remains, continue working instead of answering.

20. Supporting skills return control to the primary task. Loading or
completing a supporting skill never replaces the original user request.