#!/bin/sh
set -eu

SOURCE_URL='http://jwai-dev.jwhealthcare.com/gitea/jw-market/jw_portal_react'
EXPECTED_BRANCH='public'
IMAGE_REF=${1:-}
REMOTE=${GIT_REMOTE:-origin}

fail() { printf 'E-IMAGE-PROVENANCE: %s\n' "$1" >&2; exit 1; }
[ -n "$IMAGE_REF" ] || fail 'image reference is required'
docker image inspect "$IMAGE_REF" >/dev/null 2>&1 || fail 'image is not available locally'

label() { docker image inspect --format "{{ index .Config.Labels \"$1\" }}" "$IMAGE_REF"; }
SOURCE=$(label org.opencontainers.image.source)
REVISION=$(label org.opencontainers.image.revision)
CREATED=$(label org.opencontainers.image.created)
TREE_SHA=$(label com.jw.source.tree-sha)
BRANCH=$(label com.jw.source.branch)

for pair in "source:$SOURCE" "revision:$REVISION" "created:$CREATED" "tree-sha:$TREE_SHA" "branch:$BRANCH"; do
  value=${pair#*:}
  [ -n "$value" ] && [ "$value" != '<no value>' ] || fail "missing ${pair%%:*} label"
done

[ "$SOURCE" = "$SOURCE_URL" ] || fail 'source label mismatch'
[ "$BRANCH" = "$EXPECTED_BRANCH" ] || fail 'branch label mismatch'
printf '%s' "$CREATED" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' || fail 'created label is not UTC RFC3339'

LOCAL_REVISION=$(git rev-parse HEAD)
LOCAL_TREE=$(git rev-parse 'HEAD^{tree}')
REMOTE_SHA=$(git ls-remote "$REMOTE" "refs/heads/$EXPECTED_BRANCH" | awk 'NR == 1 {print $1}')
[ -n "$REMOTE_SHA" ] || fail 'remote public SHA is missing'
[ "$REVISION" = "$LOCAL_REVISION" ] || fail 'revision does not match local HEAD'
[ "$REVISION" = "$REMOTE_SHA" ] || fail 'revision does not match remote public'
[ "$TREE_SHA" = "$LOCAL_TREE" ] || fail 'tree label does not match local HEAD tree'

printf 'PROVENANCE_PASS image=%s revision=%s tree=%s branch=%s created=%s source=%s\n' \
  "$IMAGE_REF" "$REVISION" "$TREE_SHA" "$BRANCH" "$CREATED" "$SOURCE"
