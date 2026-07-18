INSERT INTO brand_alias (
  alias_name,
  brand_key,
  alias_type,
  alias_sources,
  note,
  created_at
)
SELECT
  ranked.brand_name,
  ranked.brand_key,
  'mart_display',
  ranked.alias_sources,
  NULL,
  NOW()
FROM (
  SELECT
    display_name.brand_key,
    display_name.brand_name,
    display_name.alias_sources,
    ROW_NUMBER() OVER (
      PARTITION BY display_name.brand_key
      ORDER BY
        CASE WHEN display_name.brand_name = display_name.brand_key THEN 0 ELSE 1 END,
        CHAR_LENGTH(display_name.brand_name),
        display_name.brand_name
    ) AS preferred_rank
  FROM (
    SELECT
      metric.brand_key,
      metric.brand_name,
      GROUP_CONCAT(DISTINCT metric.source ORDER BY metric.source SEPARATOR ',') AS alias_sources
    FROM mart_general_brand_metric AS metric
    JOIN (
      SELECT brand_key
      FROM mart_general_brand_metric
      WHERE measure = 'sales'
        AND brand_key IS NOT NULL
        AND TRIM(brand_key) <> ''
        AND brand_name IS NOT NULL
        AND TRIM(brand_name) <> ''
      GROUP BY brand_key
      HAVING COUNT(DISTINCT brand_name) > 1
    ) AS multi_key ON multi_key.brand_key = metric.brand_key
    WHERE metric.measure = 'sales'
    GROUP BY metric.brand_key, metric.brand_name
  ) AS display_name
) AS ranked
WHERE ranked.preferred_rank > 1
  AND NOT EXISTS (
    SELECT 1
    FROM mart_general_brand_metric AS canonical
    WHERE canonical.brand_key = ranked.brand_name
      AND canonical.brand_key <> ranked.brand_key
  );

INSERT INTO brand_alias (
  alias_name,
  brand_key,
  alias_type,
  alias_sources,
  note,
  created_at
)
VALUES (
  '위너프A+',
  '위너프에이플러스',
  'manual',
  NULL,
  'event_brand_scores 157행 근거',
  NOW()
);
