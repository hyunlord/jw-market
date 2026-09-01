#!/bin/sh
set -eu

required_vars="
PORTAL_API_BASE_URL
PORTAL_GOOGLE_CLIENT_ID
PORTAL_GENOS_NAVIGATION_URL
PORTAL_ROUTER_BASENAME
PORTAL_MARKET_DOCUMENT_WORKFLOW_ID
PORTAL_MARKET_ACCEPTED_UPLOAD_ENABLED
"

for name in $required_vars; do
  eval "value=\${$name-}"
  if [ -z "$value" ]; then
    echo "runtime config error: $name is required" >&2
    exit 1
  fi
  if printf '%s' "$value" | grep -q '[[:cntrl:]"\\]'; then
    echo "runtime config error: $name contains a character unsafe for JSON" >&2
    exit 1
  fi
done

case "$PORTAL_MARKET_DOCUMENT_WORKFLOW_ID" in
  *[!0-9]*|0) echo "runtime config error: PORTAL_MARKET_DOCUMENT_WORKFLOW_ID must be a positive integer" >&2; exit 1 ;;
esac

case "$PORTAL_MARKET_ACCEPTED_UPLOAD_ENABLED" in
  true|false) ;;
  *) echo "runtime config error: PORTAL_MARKET_ACCEPTED_UPLOAD_ENABLED must be true or false" >&2; exit 1 ;;
esac

if [ "$PORTAL_ROUTER_BASENAME" != "/" ]; then
  echo "runtime config error: PORTAL_ROUTER_BASENAME must be /" >&2
  exit 1
fi
