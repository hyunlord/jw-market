# JW Market crawl (tier1/tier2) image — canonical rebuild.
#
# Replaces the historical jw-market-crawl image whose build commit was never
# recorded (see BRANCH_POLICY.md: the crawl-2tier branch is not a valid build
# base). This Dockerfile assembles the exact runtime layout the crawl
# CronJobs expect, but every file comes from the reviewed extraction lineage
# on develop:
#
#   crawl/crawler/*            <- pipeline/scripts/crawler/*.py
#   crawl/agent1/score_v2.py   <- pipeline/scripts/agent_2/score_v2.py
#   crawl/agent1/corpus_loader_v2.py <- pipeline/scripts/agent_2/corpus_loader.py
#   crawl/config/drug_profiles.zip   <- docs/crawl/drug_profiles.zip
#   /opt/tier2/*               <- pipeline/scripts/crawler/tier2_*.py
#
# Build (from the repo root, linux/amd64):
#   docker build --platform linux/amd64 -f deploy/docker/crawl.Dockerfile \
#     --build-arg APP_VERSION=<tag> -t <registry>/jw-market-crawl:<tag> .

FROM python:3.11-slim

ARG APP_VERSION="local"

ENV APP_VERSION="${APP_VERSION}" \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir \
    "PyMySQL>=1.1.0" \
    "requests>=2.31.0" \
    "beautifulsoup4>=4.12.0" \
    "lxml>=5.0.0" \
    "trafilatura>=1.8.0" \
    "pandas>=2.2.0" \
    "python-dotenv>=1.0.0"

WORKDIR /work

COPY pipeline/scripts/crawler/ /work/crawl/crawler/
COPY pipeline/scripts/agent_2/score_v2.py /work/crawl/agent1/score_v2.py
COPY pipeline/scripts/agent_2/corpus_loader.py /work/crawl/agent1/corpus_loader_v2.py
COPY docs/crawl/drug_profiles.zip /work/crawl/config/drug_profiles.zip
COPY docs/crawl/_catalog.json /work/crawl/config/_catalog.json
COPY docs/crawl/search_keywords.json /work/crawl/config/search_keywords.json
COPY crawl/tier2/prompts/ /work/crawl/tier2/prompts/

RUN mkdir -p /opt/tier2 \
    && cp /work/crawl/crawler/tier2_full_scoring_runner.py /opt/tier2/ \
    && cp /work/crawl/crawler/tier2_catalog.py /opt/tier2/ \
    && cp /work/crawl/crawler/tier2_body_match_runner.py /opt/tier2/ \
    && cp /work/crawl/crawler/tier2_match_score.py /opt/tier2/ \
    && cp /work/crawl/crawler/tier2_llm_tagging.py /opt/tier2/ \
    && cp /work/crawl/crawler/tier2_hybrid_plan.py /opt/tier2/

CMD ["python", "-c", "print('jw-market-crawl canonical image; invoked via CronJob args')"]
