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
# 적용:
#   sh deploy/k8s/jw-market/apply-backend-api-resources.sh
# 확인(변경 없이 diff 만):
#   DRY_RUN=1 sh deploy/k8s/jw-market/apply-backend-api-resources.sh
set -eu

namespace=${NAMESPACE:-llmops}
deployment=jw-market-backend-api
container=jw-market-backend-api

patch='[
  {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/memory","value":"1Gi"},
  {"op":"replace","path":"/spec/template/spec/containers/0/resources/requests/cpu","value":"200m"},
  {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"3Gi"},
  {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"1"}
]'

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "[dry-run] current resources:"
  kubectl -n "$namespace" get deploy "$deployment" \
    -o jsonpath='{.spec.template.spec.containers[0].resources}{"\n"}'
  echo "[dry-run] would patch container '$container' to: requests cpu=200m memory=1Gi / limits cpu=1 memory=3Gi"
  exit 0
fi

kubectl -n "$namespace" patch deploy "$deployment" --type=json -p "$patch"
kubectl -n "$namespace" rollout status "deploy/$deployment" --timeout=300s
