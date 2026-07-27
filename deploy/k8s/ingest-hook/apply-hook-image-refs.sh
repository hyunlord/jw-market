#!/bin/sh
# jw-ingest-hook: 이미지 참조 2곳을 ★한 patch 로 동시 갱신한다.
#
# 왜 이 스크립트가 있는가
# -----------------------
# 훅의 이미지 참조는 ★두 곳이다.
#   ① container image
#   ② env INGEST_JOB_IMAGE   — 훅이 만드는 ingest Job 이 쓰는 이미지
# config.job_image() 가 env 를 우선하므로 ①만 갱신하면 ★무효다. 반대로 ②만 갱신하면
# 트리거 자신이 낡은 코드로 돈다. 그래서 ★단일 atomic patch 로 둘을 함께 옮긴다.
#
# 2026-07-27 회차의 준비 스크립트는 patch 경로에 env 인덱스(5·6)를 하드코딩했는데
# 라이브 배열은 7·8 이었고 5·6 은 MARIADB_USER/MARIADB_PASSWORD(둘 다 valueFrom
# secretKeyRef) 였다. 사람이 patch 직전에 인덱스를 다시 읽어본 것만이 그것을 막았다.
# 인덱스는 "INGEST_JOB_IMAGE 라는 항목"을 표현할 수 없으므로, 여기서는 ★이름으로
# 인덱스를 조회하고 patch 안에 ★test op 으로 그 해소 결과를 단언한다.
#   · test /metadata/resourceVersion  — 읽은 뒤 객체가 바뀌지 않았음
#   · test .../env/<i>/name           — 그 인덱스가 여전히 그 변수임
#   · test .../env/<i>/value          — 리터럴임(valueFrom 이면 경로 자체가 없어 거부됨)
# JSON Patch 는 원자적이므로 test 하나라도 실패하면 ★아무것도 쓰이지 않는다.
#
# 사용:
#   DIGEST=sha256:… APP_VERSION=<commit> sh deploy/k8s/ingest-hook/apply-hook-image-refs.sh
# 진단만(변경 없음):
#   DRY_RUN=1 DIGEST=sha256:… APP_VERSION=<commit> sh deploy/k8s/ingest-hook/apply-hook-image-refs.sh
#
# 값 결정은 호출자 책임이다. DIGEST 는 ★registry digest 여야 하며 mutable tag 를 쓰지
# 않는다(docs/runbooks/immutable_image_references.md).
set -eu

namespace=${NAMESPACE:-llmops}
deployment=${DEPLOYMENT:-jw-ingest-hook}
container=${CONTAINER:-trigger}
registry=${REGISTRY:-asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/jw-pipeline-orchestrator}

: "${DIGEST:?DIGEST is required (registry digest, e.g. sha256:...)}"
: "${APP_VERSION:?APP_VERSION is required (the commit the image was built from)}"

case "$DIGEST" in
  sha256:*) ;;
  *) echo "[error] DIGEST must be a registry digest starting with sha256: (got '$DIGEST')" >&2
     exit 2 ;;
esac

ref="$registry@$DIGEST"
spec=$(mktemp)
patch_file=$(mktemp)
trap 'rm -f "$spec" "$patch_file"' EXIT

kubectl -n "$namespace" get deploy "$deployment" -o json > "$spec"

# 해소 결과를 사람이 먼저 읽는다. 인덱스는 출력에만 나오고 결정은 이름이 한다.
python3 - "$spec" "$container" <<'PY'
import json, sys
sys.path.insert(0, ".")
from pipeline.scripts.deploy.k8s_env_patch import describe
doc = json.load(open(sys.argv[1]))
print("[info] " + describe(doc, sys.argv[2], ["APP_VERSION", "INGEST_JOB_IMAGE"]).replace("\n", "\n[info] "))
PY

# patch 생성 — 이름 부재·중복·valueFrom 이면 여기서 실패하고 아무것도 실행되지 않는다.
python3 - "$spec" "$container" "$ref" "$APP_VERSION" > "$patch_file" <<'PY'
import json, sys
sys.path.insert(0, ".")
from pipeline.scripts.deploy.k8s_env_patch import build_patch
doc = json.load(open(sys.argv[1]))
ops = build_patch(
    doc,
    container=sys.argv[2],
    image=sys.argv[3],
    env_values={"INGEST_JOB_IMAGE": sys.argv[3], "APP_VERSION": sys.argv[4]},
)
json.dump(ops, sys.stdout)
PY

echo "[info] both reference points -> $ref"
echo "[info] APP_VERSION -> $APP_VERSION"
echo "[info] patch:"
sed 's/^/[info]   /' "$patch_file"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[dry-run] no patch applied. Re-run without DRY_RUN=1 to apply."
  exit 0
fi

kubectl -n "$namespace" patch deploy "$deployment" --type=json -p "$(cat "$patch_file")"
kubectl -n "$namespace" rollout status "deploy/$deployment" --timeout=300s

# 배포 후 두 참조가 모두 새 digest 인지 확인한다. 한쪽만 맞으면 계약 위반이다.
got_image=$(kubectl -n "$namespace" get deploy "$deployment" \
  -o jsonpath="{.spec.template.spec.containers[?(@.name=='$container')].image}")
got_env=$(kubectl -n "$namespace" get deploy "$deployment" \
  -o jsonpath="{range .spec.template.spec.containers[?(@.name=='$container')].env[?(@.name=='INGEST_JOB_IMAGE')]}{.value}{end}")
echo "[verify] container image  = $got_image"
echo "[verify] INGEST_JOB_IMAGE = $got_env"
[ "$got_image" = "$ref" ] || { echo "[error] container image != $ref" >&2; exit 1; }
[ "$got_env" = "$ref" ]   || { echo "[error] INGEST_JOB_IMAGE != $ref" >&2; exit 1; }
echo "[verify] both reference points carry the deployed digest"
