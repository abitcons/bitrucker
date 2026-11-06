# Mandate status

Status: **MISSING — no verified user-approved trading mandate was found at workspace initialization.**

The existing application configuration (`strategy.json`, including `dry_run: false`) and historical research report risk parameters are not evidence of user approval for this research-to-implementation workflow.

Until the user supplies or identifies an approved mandate:
- Do not infer account capital, per-trade risk, daily loss limit, maximum exposure, leverage, allowed markets, or execution permissions.
- Do not produce actionable proposals requiring those missing constraints.
- Keep research read-only and report NO TRADE where eligibility cannot be established.
- Research proposals, if any are ever created, are not user authorization to trade.

This file is a status record, not an approval document. The research agent must not modify it to claim approval.
