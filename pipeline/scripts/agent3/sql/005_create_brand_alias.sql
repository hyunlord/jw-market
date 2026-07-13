CREATE TABLE brand_alias (
  alias_name    VARCHAR(255) NOT NULL,
  brand_key     VARCHAR(255) NOT NULL,
  alias_type    VARCHAR(32)  NOT NULL,
  alias_sources VARCHAR(64)  DEFAULT NULL,
  note          VARCHAR(255) DEFAULT NULL,
  created_at    DATETIME NOT NULL,
  PRIMARY KEY (alias_name),
  KEY idx_brand_key (brand_key)
);
