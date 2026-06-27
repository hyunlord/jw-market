# Stage 0 Evaluation Baseline

This folder contains the read-only Stage 0 evaluation harness for the chat
agent transition work.

The primary artifact is an Excel workbook where each row is one question and
each release appends a version block to the right:

```text
질문 | 카테고리 | 골드기준 | v1_답변 | v1_숫자정확 | v1_정성점수 | v1_비고 | v2_...
```

## Files

- `stage0_questions.yaml`: base 64-question evaluation set.
- `pl_questions.yaml`: PL-owned extension slot. Keep it valid YAML; empty
  `questions: []` is fine.
- `run_stage0_baseline.py`: builds the Excel workbook, `baseline_v1.json`,
  `gold_v1.json`, and markdown summary from collected raw results.

## Typical Run

Collect raw results from the deployed service or pod into JSONL with one object
per question:

```json
{"id":"REV001","ok":true,"result":{"answer":"...","tool_calls":[]}}
```

Then build the workbook:

```bash
uv run eval/run_stage0_baseline.py \
  --raw-results-jsonl /tmp/raw_results_v1.jsonl \
  --output-dir /tmp/chat_stage0_baseline_YYYYMMDD_HHMMSS \
  --version v1 \
  --system-label ux-p2-bottom
```

The script intentionally does not change the running system, database, or
deployment. It only reads collected answers and writes evaluation artifacts.
