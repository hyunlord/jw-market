#!/usr/bin/env bash
# Deploy the operational and DEV backends from one immutable image without
# replacing either workload's environment or HPA-owned replica count.
set -Eeuo pipefail

readonly CONTEXT="gke_prj-jw-agn-dev-ai-490605_asia-northeast3_kcl-jw-agn-dev-genos"
readonly NAMESPACE="llmops"
readonly CONTAINER="jw-market-backend-api"
readonly PROD_DEPLOYMENT="jw-market-backend-api"
readonly DEV_DEPLOYMENT="jw-market-backend-api-dev"

mode="dry-run"
case "${1:-}" in
  --apply) mode="apply" ;;
  --dry-run|"") ;;
  --lineage-only) mode="lineage-only" ;;
  *) echo "usage: APP_VERSION=<commit> TARGET_IMAGE=<repo@sha256:...> $0 [--lineage-only|--dry-run|--apply]" >&2; exit 2 ;;
esac

: "${APP_VERSION:?APP_VERSION must be the full candidate commit}"
SOURCE_REPO=${SOURCE_REPO:-$(pwd)}
EVIDENCE_DIR=${EVIDENCE_DIR:-/tmp/jw-market-backend-pair-deploy}
CHANGE_CAUSE=${CHANGE_CAUSE:-"Deploy backend lineage and default-period topic routing together"}
mkdir -p "$EVIDENCE_DIR"

kube() {
  kubectl --context="$CONTEXT" -n "$NAMESPACE" "$@"
}

deployment_json() {
  kube get deployment "$1" -o json
}

app_version_from() {
  python3 - "$1" "$CONTAINER" <<'PY'
import json, sys
path, name = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
container = next(item for item in data["spec"]["template"]["spec"]["containers"] if item["name"] == name)
env = {item["name"]: item.get("value", "") for item in container.get("env", [])}
print(env.get("APP_VERSION", ""))
PY
}

image_from() {
  python3 - "$1" "$CONTAINER" <<'PY'
import json, sys
path, name = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
matches = [item for item in data["spec"]["template"]["spec"]["containers"] if item["name"] == name]
if len(matches) != 1:
    raise SystemExit(f"expected one {name} container, found {len(matches)}")
print(matches[0]["image"])
PY
}

protected_hash() {
  python3 - "$1" "$CONTAINER" <<'PY'
import hashlib, json, sys
path, name = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
spec = data["spec"]
container = next(item for item in spec["template"]["spec"]["containers"] if item["name"] == name)
container["image"] = "<IMAGE>"
for item in container.get("env", []):
    if item.get("name") == "APP_VERSION":
        item.clear()
        item.update({"name": "APP_VERSION", "value": "<APP_VERSION>"})
encoded = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(encoded).hexdigest())
PY
}

dev_environment_hash() {
  python3 - "$1" "$CONTAINER" <<'PY'
import hashlib, json, sys
path, name = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
pod = data["spec"]["template"]["spec"]
container = next(item for item in pod["containers"] if item["name"] == name)
payload = {
    "env": [item for item in container.get("env", []) if item.get("name") != "APP_VERSION"],
    "envFrom": container.get("envFrom", []),
    "volumes": pod.get("volumes", []),
    "serviceAccountName": pod.get("serviceAccountName"),
}
encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(encoded).hexdigest())
PY
}

assert_dev_required_environment() {
  python3 - "$1" "$CONTAINER" <<'PY'
import json, sys
path, name = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
container = next(item for item in data["spec"]["template"]["spec"]["containers"] if item["name"] == name)
env = {item["name"]: item.get("value") for item in container.get("env", [])}
expected = {
    "ACTOR_ASSERTION_ALLOWED_KID": "portal-actor-dev-v1",
    "ACTOR_ASSERTION_ENVIRONMENT": "dev",
}
actual = {key: env.get(key) for key in expected}
if actual != expected:
    raise SystemExit(f"DEV actor environment mismatch: actual={actual} expected={expected}")
print(f"dev_actor_environment_pass values={actual}")
PY
}

assert_stable() {
  python3 - "$1" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
meta, spec, status = data["metadata"], data["spec"], data.get("status", {})
desired = int(spec.get("replicas") or 0)
actual = {
    "observedGeneration": int(status.get("observedGeneration") or 0),
    "updatedReplicas": int(status.get("updatedReplicas") or 0),
    "readyReplicas": int(status.get("readyReplicas") or 0),
    "availableReplicas": int(status.get("availableReplicas") or 0),
}
expected = {
    "observedGeneration": int(meta["generation"]),
    "updatedReplicas": desired,
    "readyReplicas": desired,
    "availableReplicas": desired,
}
if actual != expected:
    raise SystemExit(f"deployment {meta['name']} is changing or not ready: actual={actual} expected={expected}")
print(f"stable deployment={meta['name']} generation={meta['generation']} replicas={desired}")
PY
}

make_patch() {
  python3 - "$1" "$CONTAINER" "$2" "$3" "$4" <<'PY'
import json, sys
path, name, image, version, cause = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
containers = data["spec"]["template"]["spec"]["containers"]
matches = [(index, item) for index, item in enumerate(containers) if item["name"] == name]
if len(matches) != 1:
    raise SystemExit(f"expected one {name} container, found {len(matches)}")
index, container = matches[0]
env_matches = [(env_index, item) for env_index, item in enumerate(container.get("env", [])) if item.get("name") == "APP_VERSION"]
if len(env_matches) != 1:
    raise SystemExit(f"expected one APP_VERSION entry, found {len(env_matches)}")
env_index, _ = env_matches[0]
annotations = data["metadata"].get("annotations")
patch = [
    {"op": "test", "path": "/metadata/resourceVersion", "value": data["metadata"]["resourceVersion"]},
    {"op": "test", "path": f"/spec/template/spec/containers/{index}/image", "value": container["image"]},
    {"op": "replace", "path": f"/spec/template/spec/containers/{index}/image", "value": image},
    {"op": "replace", "path": f"/spec/template/spec/containers/{index}/env/{env_index}/value", "value": version},
]
if annotations is None:
    patch.append({"op": "add", "path": "/metadata/annotations", "value": {"kubernetes.io/change-cause": cause}})
else:
    annotation_op = "replace" if "kubernetes.io/change-cause" in annotations else "add"
    patch.append({"op": annotation_op, "path": "/metadata/annotations/kubernetes.io~1change-cause", "value": cause})
print(json.dumps(patch, separators=(",", ":")))
PY
}

verify_target() {
  local deployment=$1 snapshot=$2 before=$3
  [[ "$(image_from "$snapshot")" == "$TARGET_IMAGE" ]]
  [[ "$(app_version_from "$snapshot")" == "$APP_VERSION" ]]
  [[ "$(protected_hash "$snapshot")" == "$(protected_hash "$before")" ]]
  assert_stable "$snapshot"
}

prod_before="$EVIDENCE_DIR/${PROD_DEPLOYMENT}.before.json"
dev_before="$EVIDENCE_DIR/${DEV_DEPLOYMENT}.before.json"
deployment_json "$PROD_DEPLOYMENT" > "$prod_before"
deployment_json "$DEV_DEPLOYMENT" > "$dev_before"
assert_stable "$prod_before"
assert_stable "$dev_before"
assert_dev_required_environment "$dev_before"
kube get secret jw-market-audit-writer-dev -o name >/dev/null

live_source=$(app_version_from "$prod_before")
[[ "$live_source" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid operational APP_VERSION: $live_source" >&2; exit 1; }
git -C "$SOURCE_REPO" rev-parse --verify "${APP_VERSION}^{commit}" >/dev/null
git -C "$SOURCE_REPO" merge-base --is-ancestor "$live_source" "$APP_VERSION"
echo "lineage_pass live_source=$live_source candidate=$APP_VERSION"

if [[ "$mode" == "lineage-only" ]]; then
  exit 0
fi

: "${TARGET_IMAGE:?TARGET_IMAGE must be an immutable image coordinate}"
[[ "$TARGET_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] || { echo "TARGET_IMAGE must end in @sha256:<64 lowercase hex>" >&2; exit 2; }

prod_patch=$(make_patch "$prod_before" "$TARGET_IMAGE" "$APP_VERSION" "$CHANGE_CAUSE")
dev_patch=$(make_patch "$dev_before" "$TARGET_IMAGE" "$APP_VERSION" "$CHANGE_CAUSE")
kube patch deployment "$PROD_DEPLOYMENT" --type=json -p "$prod_patch" --dry-run=server -o json > "$EVIDENCE_DIR/${PROD_DEPLOYMENT}.dry.json"
kube patch deployment "$DEV_DEPLOYMENT" --type=json -p "$dev_patch" --dry-run=server -o json > "$EVIDENCE_DIR/${DEV_DEPLOYMENT}.dry.json"
[[ "$(protected_hash "$prod_before")" == "$(protected_hash "$EVIDENCE_DIR/${PROD_DEPLOYMENT}.dry.json")" ]]
[[ "$(protected_hash "$dev_before")" == "$(protected_hash "$EVIDENCE_DIR/${DEV_DEPLOYMENT}.dry.json")" ]]
[[ "$(dev_environment_hash "$dev_before")" == "$(dev_environment_hash "$EVIDENCE_DIR/${DEV_DEPLOYMENT}.dry.json")" ]]
echo "dry_run_pass target=$TARGET_IMAGE"

if [[ "$mode" == "dry-run" ]]; then
  exit 0
fi

changed=()
rollback_enabled=1
rollback_changed() {
  local exit_code=$?
  [[ $rollback_enabled -eq 1 ]] || exit "$exit_code"
  trap - ERR
  set +e
  for ((index=${#changed[@]}-1; index>=0; index--)); do
    deployment=${changed[$index]}
    before="$EVIDENCE_DIR/${deployment}.before.json"
    current="$EVIDENCE_DIR/${deployment}.rollback-current.json"
    deployment_json "$deployment" > "$current" || continue
    previous_image=$(image_from "$before")
    previous_version=$(app_version_from "$before")
    rollback_patch=$(make_patch "$current" "$previous_image" "$previous_version" "Rollback failed paired backend deployment") || continue
    kube patch deployment "$deployment" --type=json -p "$rollback_patch"
    kube rollout status "deployment/$deployment" --timeout=420s
  done
  exit "$exit_code"
}
trap rollback_changed ERR

apply_one() {
  local deployment=$1
  local before=$2
  local patch=$3
  local post="$EVIDENCE_DIR/${deployment}.post.json"
  kube patch deployment "$deployment" --type=json -p "$patch"
  changed+=("$deployment")
  kube rollout status "deployment/$deployment" --timeout=420s
  deployment_json "$deployment" > "$post"
  verify_target "$deployment" "$post" "$before"
}

apply_one "$PROD_DEPLOYMENT" "$prod_before" "$prod_patch"
apply_one "$DEV_DEPLOYMENT" "$dev_before" "$dev_patch"
[[ "$(dev_environment_hash "$dev_before")" == "$(dev_environment_hash "$EVIDENCE_DIR/${DEV_DEPLOYMENT}.post.json")" ]]
assert_dev_required_environment "$EVIDENCE_DIR/${DEV_DEPLOYMENT}.post.json"
[[ "$(image_from "$EVIDENCE_DIR/${PROD_DEPLOYMENT}.post.json")" == "$(image_from "$EVIDENCE_DIR/${DEV_DEPLOYMENT}.post.json")" ]]
[[ "$(app_version_from "$EVIDENCE_DIR/${PROD_DEPLOYMENT}.post.json")" == "$(app_version_from "$EVIDENCE_DIR/${DEV_DEPLOYMENT}.post.json")" ]]
rollback_enabled=0
trap - ERR
echo "paired_deploy_pass image=$TARGET_IMAGE app_version=$APP_VERSION"
