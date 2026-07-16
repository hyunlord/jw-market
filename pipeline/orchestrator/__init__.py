"""Single entry point for the monthly pipeline chain.

``python -m pipeline.orchestrator run`` plans and executes the six-stage
chain (cache -> forecast -> strength -> shortlong -> events -> elements) in
full, incremental, or partial form. The orchestrator never reimplements
builder logic: every stage shells out to the canonical builder CLI, and every
builder-side gate stays in force. See RUNBOOK_MONTHLY.md for operations.
"""
