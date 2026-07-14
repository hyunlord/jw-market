#!/usr/bin/env bash
set -euo pipefail
export ROOT=${ROOT:?}; export CM=${CM:?}; export IMG=${IMG:?}
echo "LOOP_START $(date -u +%FT%TZ) ROOT=$ROOT" | tee -a "$ROOT/loop.log"
for CHUNK in 7 8 9 10 11 12 13 14 15 16 17; do
  [[ -f "$ROOT/ABORT" ]] && { echo "ABORT_PRESENT before chunk $CHUNK" | tee -a "$ROOT/loop.log"; exit 2; }
  if [[ -f "$ROOT/state/chunk_${CHUNK}.status" ]] && grep -q PASS "$ROOT/state/chunk_${CHUNK}.status"; then echo "SKIP_PASS chunk=$CHUNK" | tee -a "$ROOT/loop.log"; continue; fi
  echo "RUN_CHUNK $CHUNK $(date -u +%FT%TZ)" | tee -a "$ROOT/loop.log"
  if ! "$ROOT/run_one_chunk.sh" "$CHUNK" >> "$ROOT/loop.log" 2>&1; then echo "LOOP_ABORT chunk=$CHUNK $(date -u +%FT%TZ)" | tee -a "$ROOT/loop.log"; exit 3; fi
  echo "DONE_CHUNK $CHUNK $(date -u +%FT%TZ)" | tee -a "$ROOT/loop.log"
done
touch "$ROOT/COMPLETE"; echo "LOOP_COMPLETE $(date -u +%FT%TZ)" | tee -a "$ROOT/loop.log"
