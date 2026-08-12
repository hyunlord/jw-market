-- ROW_TOPIC_STEP3_5_IMPL_20260812: approved idempotent one-pointer bootstrap.
-- UTC naive timestamp is supplied by the caller as @bootstrap_now_utc.

INSERT INTO jw_brand_activity_stage.row_topic_taxonomy_active_release_v1
  (pointer_name, active_release_id, generation, updated_at, updated_by)
VALUES
  ('brand_activity_keyword', NULL, 0, @bootstrap_now_utc, 'ROW_TOPIC_STEP3_5_IMPL_20260812')
ON DUPLICATE KEY UPDATE pointer_name = VALUES(pointer_name);
