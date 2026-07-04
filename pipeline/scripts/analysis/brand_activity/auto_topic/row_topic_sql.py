from __future__ import annotations


def assignment_table_ddl(schema: str) -> str:
    """Return the gated DDL for normalized row-topic assignments."""
    return f"""
CREATE TABLE IF NOT EXISTS {schema}.row_topic_assignment (
  row_id BIGINT UNSIGNED NOT NULL,
  scope_id VARCHAR(128) NOT NULL,
  brand VARCHAR(255) NOT NULL,
  topic_id VARCHAR(128) NOT NULL,
  topic_set_version VARCHAR(128) NOT NULL,
  prompt_version VARCHAR(64) NOT NULL,
  assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  batch_id VARCHAR(160) NOT NULL,
  PRIMARY KEY (row_id, topic_id, topic_set_version),
  KEY idx_row_topic_scope_brand_version (scope_id, brand, topic_set_version),
  KEY idx_row_topic_topic_version (topic_id, topic_set_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
""".strip()


def compatible_share_view_sql(schema: str) -> str:
    """Return a legacy topic_shares-compatible aggregation view."""
    return f"""
CREATE OR REPLACE VIEW {schema}.row_topic_assignment_share_view AS
SELECT
  a.topic_set_version,
  a.prompt_version,
  a.scope_id,
  a.brand,
  a.topic_id,
  COUNT(DISTINCT a.row_id) AS affected_row_count,
  brand_total.brand_total_rows,
  ROUND(COUNT(DISTINCT a.row_id) * 100.0 / brand_total.brand_total_rows, 2) AS share_pct
FROM {schema}.row_topic_assignment a
JOIN (
  SELECT
    scope_id,
    brand,
    topic_set_version,
    COUNT(DISTINCT row_id) AS brand_total_rows
  FROM (
    SELECT
      topic_scope.scope_id,
      product_name AS brand,
      row_id_source.topic_set_version,
      k.id AS row_id
    FROM {schema}.km_keyword_event_stage k
    JOIN {schema}.mart_brand_activity_topics topic_scope
      ON JSON_CONTAINS(topic_scope.atc4_values, JSON_QUOTE(k.therapeutic_class), '$')
    JOIN (
      SELECT DISTINCT scope_id, brand, topic_set_version
      FROM {schema}.row_topic_assignment
    ) row_id_source
      ON row_id_source.scope_id = topic_scope.scope_id
     AND row_id_source.brand = k.product_name
  ) scoped_rows
  GROUP BY scope_id, brand, topic_set_version
) brand_total
  ON brand_total.scope_id = a.scope_id
 AND brand_total.brand = a.brand
 AND brand_total.topic_set_version = a.topic_set_version
GROUP BY
  a.topic_set_version,
  a.prompt_version,
  a.scope_id,
  a.brand,
  a.topic_id,
  brand_total.brand_total_rows;
""".strip()
