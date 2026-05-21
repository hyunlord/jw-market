-- Phase 16-G-4-Fix-CacheSplitCleanup
-- Drop the reconciled legacy response cache after split-cache validation.

DROP TABLE IF EXISTS response_store_legacy;
