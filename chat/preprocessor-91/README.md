# preprocessor-91 source and deployment contract

`preprocessor-91` runs the shared image `mnc/doc-parser-preprocessor:1.3.8.1-gpu-libpath-memfix-fv` and mounts `src/preprocessor.py` from the `preprocessor-91` ConfigMap. The source in this directory is therefore the deployment artifact; this change does not build or replace the image.

The imported live baseline had SHA256 `929448d6e8a3cf01d040db276036e79b619f9103356b0d69e9ca5e942b1fa7cb`.

## Parallel extraction

- `PREPROC_PAGE_WORKERS` bounds a spawn-based process pool by the container's cgroup CPU quota.
- `PREPROC_PAGE_WORKER_THREADS` bounds each worker's native math/layout threads so the pool cannot oversubscribe the CPU quota.
- Each process opens its own PyMuPDF document. No `fitz.Document` instance is shared between processes.
- Workers only extract page Markdown. Heading state, chunk packing, bbox lookup, and response assembly remain single-process and source ordered.
- Any page extraction error rejects the whole Markdown result. Partial page sets are never returned.

The 4-CPU deployment uses two page processes with two native threads each. On the 249-page benchmark, four single-threaded processes took 114.97 seconds while the 2x2 split took 95.10 seconds with identical output hashes. The lower process count avoids process startup and duplicated document-open overhead while still parallelizing independent pages.

## Large PDF admission

PDFs at or above either `PREPROC_LARGE_PDF_MIN_PAGES` or `PREPROC_LARGE_PDF_MIN_BYTES` acquire a pod-local `flock`. This covers both gunicorn workers while allowing small files to bypass the queue. The lock is held by the blocking extraction thread, so coroutine cancellation cannot release it while parsing continues.

## Deployment

1. Build the ConfigMap from `src/preprocessor.py` while preserving the existing `pip.conf` key.
2. Apply `deploy/preprocessor-91-patch.yaml` as a strategic merge patch.
3. Add a source SHA annotation to the pod template to force a rollout.
4. Verify the mounted file SHA, 4-CPU cgroup quota, 16GiB memory limit, Ready state, restart count, and OOM counters.

The deployment patch intentionally keeps the existing image, replicas, 300-second caller timeout, and file-size guard unchanged. Rollback restores the pre-change ConfigMap source and the prior 1-CPU/8Gi resource block before restarting the pod.
