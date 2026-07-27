#!/bin/sh
# jw-market-backend-api: 컨테이너 리소스 정본 적용 (patch 방식)
#
# 왜 Deployment 전체 매니페스트가 아니라 patch 인가
# ---------------------------------------------------
# jw-market-backend-api 는 저장소에 Deployment 매니페스트가 없다(2026-07-27 기준).
# 라이브 Deployment 는 두 가지를 **다른 주체가** 소유한다:
#   1) image     — 릴리스 절차가 `kubectl set image` 로 관리한다.
#                  전체 매니페스트를 만들어 apply 하면 매니페스트에 박힌 image 로
#                  **롤백되어 배포가 되돌아간다**.
#   2) replicas  — HorizontalPodAutoscaler 가 소유한다.
#                  매니페스트에 replicas 를 넣고 apply 하면 HPA 와 충돌한다.
# 따라서 우리가 소유하는 항목(리소스 요청/상한)만 patch 로 고정한다.
# 전체 매니페스트 도입은 image/replicas 소유권 정리가 선행되어야 하며 PL 결정 사항이다.
#
# 값 근거 (ops_issues_findings_20260727_1510)
#   memory request 512Mi -> 1Gi
#     - 정상상태 파드 사용량 649Mi(app 581Mi + istio sidecar 68Mi)가 기존 request 640Mi 를 넘겨
#       HPA memory utilization 이 101% 로 상시 포화 -> maxReplicas 8 에 영구 고착이었다.
#     - 1Gi 로 올리면 파드 request 1152Mi, utilization 56.3% 가 되어 정상 범위로 들어온다.
#   cpu request/limit, memory limit 은 변경하지 않는다(기존 값 그대로 명시해 고정).
#
# ★ 2026-07-27 수정 (alert_recheck 라운드) — D-1
#   - 이전 판은 patch 경로에 containers/0 인덱스를 **하드코딩**했다.
#     이 Deployment 에는 istio 가 사이드카를 주입하므로 컨테이너 배열 구성이
#     바뀌면 **엉뚱한 컨테이너에 리소스를 덮어쓸 위험**이 있었다.
#     -> 컨테이너 **이름으로 인덱스를 조회**해서 patch 하도록 변경.
#   - 선언만 하고 쓰지 않던 container 변수를 실제로 사용하도록 수정.
#   - --type=json 의 replace 는 **경로가 없으면 실패**하므로, 대상 경로 존재 여부를
#     확인해 없으면 add 로 자동 전환하는 preflight 추가.
#
# 적용:
#   sh deploy/k8s/jw-market/apply-backend-api-resources.sh
# 확인(변경 없이 진단 + 서버 검증만):
#   DRY_RUN=1 sh deploy/k8s/jw-market/apply-backend-api-resources.sh
set -eu

namespace=${NAMESPACE:-llmops}
deployment=${DEPLOYMENT:-jw-market-backend-api}
container=${CONTAINER:-jw-market-backend-api}

req_cpu=200m
req_mem=1Gi
lim_cpu=1
lim_mem=3Gi

# ---- 컨테이너 이름 -> 인덱스 조회 (인덱스 하드코딩 금지)
idx=$(kubectl -n "$namespace" get deploy "$deployment" -o json | python3 -c "
import sys,json
d=json.load(sys.stdin)
cs=d['spec']['template']['spec']['containers']
name='$container'
for i,c in enumerate(cs):
    if c['name']==name:
        print(i); sys.exit(0)
sys.stderr.write('container %r not found; present=%s\n' % (name,[c['name'] for c in cs]))
sys.exit(1)
")
echo "[info] container '$container' -> containers[$idx]  (이름 조회, 하드코딩 아님)"

# ---- preflight: 대상 경로 존재 여부 -> replace / add 결정
op_for() {  # $1=requests|limits  $2=cpu|memory
  v=$(kubectl -n "$namespace" get deploy "$deployment" \
        -o "jsonpath={.spec.template.spec.containers[$idx].resources.$1.$2}" 2>/dev/null || true)
  [ -n "$v" ] && echo replace || echo add
}
op_rc=$(op_for requests cpu); op_rm=$(op_for requests memory)
op_lc=$(op_for limits cpu);   op_lm=$(op_for limits memory)

cur=$(kubectl -n "$namespace" get deploy "$deployment" \
        -o "jsonpath={.spec.template.spec.containers[$idx].resources}" 2>/dev/null || true)
echo "[info] current resources: ${cur:-<none>}"
echo "[info] ops: requests.cpu=$op_rc requests.memory=$op_rm limits.cpu=$op_lc limits.memory=$op_lm"

# ★ 2026-07-27 추가 (deploy-script-index-safety 라운드)
#   인덱스를 이름으로 조회하는 것만으로는 조회와 patch 사이의 간격이 남는다. istio 가
#   그 사이에 사이드카를 재주입하면 같은 인덱스가 다른 컨테이너를 가리키고, patch 는
#   그대로 성공한다. 그래서 patch 첫 op 으로 ★해당 인덱스가 여전히 그 이름인지 단언한다.
#   JSON Patch 는 원자적이므로 test 가 실패하면 아무 op 도 적용되지 않는다.
guard='{"op":"test","path":"/spec/template/spec/containers/'"$idx"'/name","value":"'"$container"'"},'

# resources 자체가 없으면 빈 객체를 먼저 만든다
pre=""
[ -z "$cur" ] && pre='{"op":"add","path":"/spec/template/spec/containers/'"$idx"'/resources","value":{}},'

patch='['"$guard$pre"'
  {"op":"'"$op_rc"'","path":"/spec/template/spec/containers/'"$idx"'/resources/requests/cpu","value":"'"$req_cpu"'"},
  {"op":"'"$op_rm"'","path":"/spec/template/spec/containers/'"$idx"'/resources/requests/memory","value":"'"$req_mem"'"},
  {"op":"'"$op_lc"'","path":"/spec/template/spec/containers/'"$idx"'/resources/limits/cpu","value":"'"$lim_cpu"'"},
  {"op":"'"$op_lm"'","path":"/spec/template/spec/containers/'"$idx"'/resources/limits/memory","value":"'"$lim_mem"'"}
]'

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[dry-run] target: containers[$idx] ('$container')"
  echo "[dry-run] desired: requests cpu=$req_cpu memory=$req_mem / limits cpu=$lim_cpu memory=$lim_mem"
  echo "[dry-run] payload:"
  echo "$patch"
  echo "[dry-run] server-side 검증(실제 변경 없음) 결과 resources:"
  kubectl -n "$namespace" patch deploy "$deployment" --type=json -p "$patch" --dry-run=server \
    -o "jsonpath={.spec.template.spec.containers[$idx].resources}{\"\n\"}"
  exit 0
fi

kubectl -n "$namespace" patch deploy "$deployment" --type=json -p "$patch"
kubectl -n "$namespace" rollout status "deploy/$deployment" --timeout=300s
