#!/bin/sh
# 워크로드 하나의 이미지 참조를 ★이름 기반으로 갱신한다 (kind 무관).
#
# 왜 별도 스크립트인가
# --------------------
# apply-hook-image-refs.sh 는 jw-ingest-hook 전용이다. 그 워크로드는 이미지 참조가
# ★2곳(container image + INGEST_JOB_IMAGE env)이라 둘을 한 patch 로 묶는 것이 계약이고,
# 스크립트가 그 env 이름을 알고 있다. 다른 워크로드는 참조 지점 수와 이름이 다르다.
# 예: cronjob/brand-activity-row-topic-monthly 는 ★container image 1곳뿐이고 env 에
# 이미지 참조가 없다. 그 워크로드에 hook 전용 스크립트를 쓰면 없는 env 를 찾다 실패한다.
#
# 그래서 이 스크립트는 ★대상과 참조 지점을 인자로 받는다. 결정 방식은 동일하다:
#   · container 인덱스를 ★이름으로 조회 (인덱스 하드코딩 없음)
#   · pod template 경로를 ★kind 에서 유도 (Deployment 와 CronJob 이 다르다)
#   · patch 안에 ★test op — resourceVersion · container 이름 · 현재 image/value
#   · valueFrom 항목은 ★거부 (secret 참조를 리터럴로 바꾸지 않는다)
#   · ★단일 atomic patch. 참조가 여러 곳이면 전부 한 patch 에 들어간다
#
# 사용:
#   KIND=cronjob NAME=brand-activity-row-topic-monthly CONTAINER=row-topic-monthly \
#   IMAGE=<repo>@sha256:... \
#   sh deploy/k8s/apply-image-ref.sh
#
# env 도 함께 갱신하려면 (이름=값, 콤마 구분):
#   ENV_VALUES="APP_VERSION=abc123,OTHER=x" ...
#
# 진단만 (★변경 0):
#   DRY_RUN=1 ... sh deploy/k8s/apply-image-ref.sh
#
# ★CronJob 을 대상으로 할 때: 이 스크립트는 image/env 만 patch 한다. schedule ·
#   suspend · status(lastScheduleTime 등) 는 patch 본문에 들어가지 않으므로 불변이다.
#   CronJob 을 삭제/재생성하지 않는다 — 예약된 발화를 잃는다.
set -eu

namespace=${NAMESPACE:-llmops}
: "${KIND:?KIND is required (e.g. deploy, cronjob, statefulset)}"
: "${NAME:?NAME is required}"
: "${CONTAINER:?CONTAINER is required (container name, not an index)}"
: "${IMAGE:?IMAGE is required (full ref with a registry digest)}"

case "$IMAGE" in
  *@sha256:*) ;;
  *) echo "[error] IMAGE must carry a registry digest (…@sha256:…), not a mutable tag." >&2
     echo "        see docs/runbooks/immutable_image_references.md" >&2
     exit 2 ;;
esac

spec=$(mktemp); patch_file=$(mktemp)
trap 'rm -f "$spec" "$patch_file"' EXIT

kubectl -n "$namespace" get "$KIND" "$NAME" -o json > "$spec"

echo "[info] target: $KIND/$NAME  container=$CONTAINER  ns=$namespace"
python3 - "$spec" "$CONTAINER" "${ENV_VALUES:-}" <<'PY'
import json, sys
sys.path.insert(0, ".")
from pipeline.scripts.deploy.k8s_env_patch import describe
doc = json.load(open(sys.argv[1]))
names = [kv.split("=", 1)[0] for kv in sys.argv[3].split(",") if kv]
print("[info] " + describe(doc, sys.argv[2], names).replace("\n", "\n[info] "))
PY

python3 - "$spec" "$CONTAINER" "$IMAGE" "${ENV_VALUES:-}" > "$patch_file" <<'PY'
import json, sys
sys.path.insert(0, ".")
from pipeline.scripts.deploy.k8s_env_patch import build_patch
doc = json.load(open(sys.argv[1]))
env_values = {}
for kv in sys.argv[4].split(","):
    if not kv:
        continue
    if "=" not in kv:
        raise SystemExit(f"ENV_VALUES entry {kv!r} is not name=value")
    k, v = kv.split("=", 1)
    env_values[k] = v
json.dump(build_patch(doc, container=sys.argv[2], image=sys.argv[3], env_values=env_values),
          sys.stdout, indent=2)
PY

echo "[info] image -> $IMAGE"
[ -n "${ENV_VALUES:-}" ] && echo "[info] env   -> ${ENV_VALUES}"
echo "[info] patch:"
sed 's/^/[info]   /' "$patch_file"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[dry-run] nothing applied. The patch above is what a real run would send."
  echo "[dry-run] re-run without DRY_RUN=1 to apply."
  exit 0
fi

kubectl -n "$namespace" patch "$KIND" "$NAME" --type=json -p "$(cat "$patch_file")"

# CronJob 은 rollout status 대상이 아니다. Deployment/StatefulSet 만 대기한다.
case "$KIND" in
  deploy|deployment|deployments|statefulset|statefulsets|sts|daemonset|daemonsets|ds)
    kubectl -n "$namespace" rollout status "$KIND/$NAME" --timeout=300s ;;
  *) echo "[info] $KIND has no rollout to wait on; the next scheduled run picks up the image." ;;
esac

# 배포 후 확인: 참조 지점이 실제로 새 값인가.
got=$(kubectl -n "$namespace" get "$KIND" "$NAME" -o json | python3 -c "
import json,sys
sys.path.insert(0, '.')
from pipeline.scripts.deploy.k8s_env_patch import resolve_container_index, _containers
doc=json.load(sys.stdin)
ci=resolve_container_index(doc, '$CONTAINER')
print(_containers(doc)[ci]['image'])
")
echo "[verify] container image = $got"
[ "$got" = "$IMAGE" ] || { echo "[error] image != $IMAGE" >&2; exit 1; }
echo "[verify] the image reference carries the deployed digest"
