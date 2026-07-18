"""Incremental ingest hook: webhook -> G3 structural validation -> k8s Job.

This package is the jw-market side of the JW_Input_Detection_Contract_v2 flow.
The jw-data-input site fires one webhook per confirmed submission; the trigger
service records it in ``ingest_ledger`` (idempotent), serialises per category,
and launches an incremental load Job whose first step is always G3.

Nothing here touches the serving backend (jw-market-backend-api) and nothing
activates itself: the k8s resources ship suspended / at zero replicas and the
production ledger table is only created on explicit activation (PL gate).
"""
