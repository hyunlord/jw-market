# BQ Screen

Offline answer-screening module for BQ capture artifacts.

Input: a capture directory with `screen/*.raw.json` files containing `case`,
`status`, `elapsed_s`, `error`, and `response.text` plus optional
`response.trace.tools_called`.

Run:

```bash
python -m scripts.bq_screen.runner \
  --capture-dir /private/tmp/bq4_capture_screen_20260704T045521Z \
  --out /tmp/bq_screen_results.json
```

This package is an evaluation tool only. It must not be imported by the
serving answer path.
