#!/bin/sh
set -eu

SOURCE_URL='http://jwai-dev.jwhealthcare.com/gitea/jw-market/jw_portal_react'
EXPECTED_BRANCH='public'
IMAGE_REF=${1:-}
REMOTE=${GIT_REMOTE:-origin}

fail() { printf 'E-BUILD-PROVENANCE: %s\n' "$1" >&2; exit 1; }
[ -n "$IMAGE_REF" ] || fail 'image reference is required'

BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)
[ "$BRANCH" = "$EXPECTED_BRANCH" ] || fail "branch must be $EXPECTED_BRANCH (got ${BRANCH:-detached})"
[ -z "$(git status --porcelain --untracked-files=normal)" ] || fail 'working tree is dirty'

REVISION=$(git rev-parse HEAD)
TREE_SHA=$(git rev-parse 'HEAD^{tree}')
REMOTE_SHA=$(git ls-remote "$REMOTE" "refs/heads/$EXPECTED_BRANCH" | awk 'NR == 1 {print $1}')
[ -n "$REMOTE_SHA" ] || fail 'remote public SHA is missing'
[ "$REMOTE_SHA" = "$REVISION" ] || fail "remote public SHA mismatch ($REMOTE_SHA != $REVISION)"
CREATED=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

for value in "$SOURCE_URL" "$REVISION" "$CREATED" "$TREE_SHA" "$BRANCH"; do
  [ -n "$value" ] || fail 'a required label value is empty'
done

docker build --platform linux/amd64 -f Dockerfile.portal \
  --build-arg "OCI_SOURCE=$SOURCE_URL" \
  --build-arg "OCI_REVISION=$REVISION" \
  --build-arg "OCI_CREATED=$CREATED" \
  --build-arg "JW_SOURCE_TREE_SHA=$TREE_SHA" \
  --build-arg "JW_SOURCE_BRANCH=$BRANCH" \
  -t "$IMAGE_REF" .

# The canonical build is not complete until the produced image is verified.
"$(dirname "$0")/verify-image-provenance.sh" "$IMAGE_REF"

printf 'IMAGE_REF=%s\nREVISION=%s\nTREE_SHA=%s\nBRANCH=%s\nCREATED=%s\nSOURCE=%s\n' \
  "$IMAGE_REF" "$REVISION" "$TREE_SHA" "$BRANCH" "$CREATED" "$SOURCE_URL"
