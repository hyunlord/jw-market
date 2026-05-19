.PHONY: help build init load-ubist load-iqvia enrich verify all clean

PROJECT_ROOT := $(shell git rev-parse --show-toplevel 2>/dev/null || pwd)
IMAGE := jw-market-pipeline:local
DATA := $(PROJECT_ROOT)/data
OUTPUT := $(PROJECT_ROOT)/output
CATALOG := $(PROJECT_ROOT)/catalog

help:
	@echo "Targets:"
	@echo "  build         - Docker image build"
	@echo "  init          - migration status + apply --all"
	@echo "  load-ubist    - UBIST -> output/ubist parquet"
	@echo "  load-iqvia    - IQVIA -> MariaDB raw"
	@echo "  enrich        - Layer 2 enriched_fact"
	@echo "  verify        - read-only pipeline checks"
	@echo "  all           - init + load-ubist + load-iqvia + enrich + verify"
	@echo "  clean         - remove Python cache files only"

build:
	cd $(PROJECT_ROOT) && docker build -t $(IMAGE) -f pipeline/Dockerfile .

init:
	python pipeline/scripts/run_migration.py status
	python pipeline/scripts/run_migration.py apply --all

load-ubist:
	python pipeline/scripts/etl/ubist_parquet_loader.py --all --truncate

load-iqvia:
	python pipeline/scripts/etl/iqvia_loader.py --all

enrich:
	python pipeline/scripts/etl/layer2_enrich.py --all

verify:
	python pipeline/scripts/run_migration.py status
	@ls -d $(OUTPUT)/ubist/year=*/month=*/ 2>/dev/null | wc -l | xargs echo "ubist partitions:"
	@ls -d $(OUTPUT)/enriched/ml_id=*/ 2>/dev/null | wc -l | xargs echo "enriched partitions:"
	@test -f $(OUTPUT)/ubist/_manifest.json && echo "ubist manifest: present"

all: init load-ubist load-iqvia enrich verify

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete
