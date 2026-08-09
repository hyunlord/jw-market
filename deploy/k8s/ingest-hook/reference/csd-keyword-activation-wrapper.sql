-- IQVIA CSD keyword atomic activation boundary.
-- Live table access stays with the existing localhost definer; ingest Jobs get
-- EXECUTE only through jw-csd-channel-activator.

CREATE DATABASE `jw_csd_keyword_control`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE `jw_csd_keyword_rollback_raw`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE `jw_csd_keyword_rollback_stage`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

DELIMITER $$

CREATE DEFINER='jw_csd_channel_definer'@'localhost'
PROCEDURE `jw_csd_keyword_control`.`csd_keyword_atomic_publish`(
  IN p_run_id VARCHAR(20),
  IN p_raw_schema VARCHAR(64),
  IN p_stage_schema VARCHAR(64),
  IN p_raw_rows BIGINT UNSIGNED,
  IN p_stage_rows BIGINT UNSIGNED,
  IN p_period_count BIGINT UNSIGNED,
  IN p_min_period CHAR(7),
  IN p_max_period CHAR(7)
)
MODIFIES SQL DATA
SQL SECURITY DEFINER
COMMENT 'Validate and atomically publish a run-scoped keyword raw+stage pair'
BEGIN
  DECLARE v_raw_candidate VARCHAR(64);
  DECLARE v_stage_candidate VARCHAR(64);
  DECLARE v_raw_old VARCHAR(64);
  DECLARE v_stage_old VARCHAR(64);
  DECLARE v_table_count INT DEFAULT 0;

  IF p_run_id IS NULL OR p_run_id NOT REGEXP '^[0-9]{20}$' THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='invalid keyword run_id; expected 20 digits';
  END IF;
  IF @@session.time_zone <> '+00:00' THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='keyword session time_zone must be +00:00';
  END IF;
  IF IS_USED_LOCK('jw_ingest_csd_keyword_activation') <> CONNECTION_ID() THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='keyword writer lock is not owned by this connection';
  END IF;
  IF p_raw_schema <> 'jw_brand_activity_raw_stage'
     OR p_stage_schema <> 'jw_brand_activity_stage' THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='keyword live schema is not approved';
  END IF;
  IF p_raw_rows < 1 OR p_stage_rows < 1 OR p_period_count < 1
     OR p_min_period NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
     OR p_max_period NOT REGEXP '^[0-9]{4}-(0[1-9]|1[0-2])$'
     OR p_min_period > p_max_period THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='keyword candidate evidence is invalid';
  END IF;

  SET v_raw_candidate = CONCAT('jw_brand_activity_keyword_', p_run_id, '_raw');
  SET v_stage_candidate = CONCAT('jw_brand_activity_keyword_', p_run_id, '_stage');
  SET v_raw_old = CONCAT('raw_keyword_events__old_', p_run_id);
  SET v_stage_old = CONCAT('km_keyword_event_stage__old_', p_run_id);

  SELECT COUNT(*) INTO v_table_count
  FROM information_schema.TABLES
  WHERE (TABLE_SCHEMA=p_raw_schema AND TABLE_NAME='raw_keyword_events')
     OR (TABLE_SCHEMA=p_stage_schema AND TABLE_NAME='km_keyword_event_stage')
     OR (TABLE_SCHEMA=v_raw_candidate AND TABLE_NAME='raw_keyword_events')
     OR (TABLE_SCHEMA=v_stage_candidate AND TABLE_NAME='km_keyword_event_stage');
  IF v_table_count <> 4 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='keyword live/candidate table set is absent or partial';
  END IF;

  SELECT COUNT(*) INTO v_table_count
  FROM information_schema.TABLES
  WHERE (TABLE_SCHEMA='jw_csd_keyword_rollback_raw' AND TABLE_NAME=v_raw_old)
     OR (TABLE_SCHEMA='jw_csd_keyword_rollback_stage' AND TABLE_NAME=v_stage_old);
  IF v_table_count <> 0 THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='keyword rollback table already exists';
  END IF;

  SET @kw_sql = CONCAT(
    'SELECT COUNT(*) INTO @kw_raw_rows FROM `',
    v_raw_candidate, '`.`raw_keyword_events`');
  EXECUTE IMMEDIATE @kw_sql;
  SET @kw_sql = CONCAT(
    'SELECT COUNT(*),COUNT(DISTINCT `period_ym`),MIN(`period_ym`),MAX(`period_ym`) ',
    'INTO @kw_stage_rows,@kw_period_count,@kw_min_period,@kw_max_period FROM `',
    v_stage_candidate, '`.`km_keyword_event_stage`');
  EXECUTE IMMEDIATE @kw_sql;
  IF @kw_raw_rows <> p_raw_rows
     OR @kw_stage_rows <> p_stage_rows
     OR @kw_period_count <> p_period_count
     OR NOT (@kw_min_period <=> p_min_period)
     OR NOT (@kw_max_period <=> p_max_period) THEN
    SIGNAL SQLSTATE '45000'
      SET MESSAGE_TEXT='keyword candidate evidence changed before publish';
  END IF;

  SET @kw_sql = CONCAT(
    'RENAME TABLE `',p_raw_schema,'`.`raw_keyword_events` TO `',
    'jw_csd_keyword_rollback_raw`.`',v_raw_old,'`, `',
    v_raw_candidate,'`.`raw_keyword_events` TO `',
    p_raw_schema,'`.`raw_keyword_events`, `',
    p_stage_schema,'`.`km_keyword_event_stage` TO `',
    'jw_csd_keyword_rollback_stage`.`',v_stage_old,'`, `',
    v_stage_candidate,'`.`km_keyword_event_stage` TO `',
    p_stage_schema,'`.`km_keyword_event_stage`');
  EXECUTE IMMEDIATE @kw_sql;

  SELECT 'applied' AS publish_state,
         p_run_id AS run_id,
         p_raw_rows AS raw_rows,
         p_stage_rows AS stage_rows,
         p_period_count AS period_count,
         p_min_period AS min_period,
         p_max_period AS max_period;
END$$

DELIMITER ;

GRANT SELECT, INSERT, CREATE, DROP
  ON `jw\_brand\_activity\_keyword\_%`.*
  TO 'jw_csd_channel_definer'@'localhost';
GRANT SELECT, INSERT, CREATE, DROP
  ON `jw_csd_keyword_rollback_raw`.*
  TO 'jw_csd_channel_definer'@'localhost';
GRANT SELECT, INSERT, CREATE, DROP
  ON `jw_csd_keyword_rollback_stage`.*
  TO 'jw_csd_channel_definer'@'localhost';
GRANT SELECT, INSERT, CREATE, DROP
  ON `jw_brand_activity_raw_stage`.`raw_keyword_events`
  TO 'jw_csd_channel_definer'@'localhost';
GRANT SELECT, INSERT, CREATE, DROP
  ON `jw_brand_activity_stage`.`km_keyword_event_stage`
  TO 'jw_csd_channel_definer'@'localhost';
GRANT EXECUTE
  ON PROCEDURE `jw_csd_keyword_control`.`csd_keyword_atomic_publish`
  TO 'jw_csd_channel_activator'@'10.13.128.0/255.255.240.0';
