# monitoring — 알림 경로 (★ 아직 적용 안 됨)

작성: 2026-07-27 · 근거: `ops_issues_findings_20260727_1510`

## 문제

kube-prometheus-stack 의 Alertmanager 가 **모든 알림을 버리고 있다.**

```yaml
route:
  receiver: "null"      # 차트 기본값 그대로
receivers:
  - name: "null"
```

- 차트(kube-prometheus-stack 80.4.1) **기본값이며 누가 바꾼 것이 아니다** —
  Alertmanager CR `generation: 1`, Helm 릴리스 리비전 `v1` 하나뿐(2026-04-21 설치 이후 무변경).
- 결과: 2026-07-27 기준 **52건이 발화 중인데 전부 폐기**되고 있었다.
  - `KubeJobFailed` **ns=llmops × 19**  ← UBIST OOM · cache-warm 실패가 여기 포함
  - `KubeHpaMaxedOut` ns=llmops × 1     ← backend/chat HPA 고착
  - `KubePodNotReady`, `KubeContainerWaiting`, `KubeDeploymentReplicasMismatch` 등
- 즉 **규칙은 정상 동작했고, 전달 경로만 없었다.**

## 규칙 커버리지

기존 25개 PrometheusRule 로 아래는 이미 커버된다.

| 알림 | 존재 | for |
|---|---|---|
| KubeJobFailed | O | 15m |
| KubeJobNotCompleted | O | – |
| KubePodCrashLooping | O | 15m |
| KubePodNotReady | O | 15m |
| KubeContainerWaiting | O | 1h |
| KubeHpaMaxedOut | O | 15m |
| **KubeContainerOOMKilled** | **X (없음)** | – |

→ OOM 규칙만 공백이라 `prometheusrule-jw-market-oom.yaml` 로 추가한다.

## 파일

| 파일 | 상태 | 설명 |
|---|---|---|
| `prometheusrule-jw-market-oom.yaml` | **미적용** | 누락된 OOMKilled 규칙. ns=llmops(우리 소관)에 배치. PromQL 검증 완료 |
| `alertmanagerconfig-jw-market.yaml` | **미적용 · 수신처 PLACEHOLDER** | 알림 전달 경로. 수신처 확정 전 apply 금지 |

## 왜 Alertmanager 기본 설정을 고치지 않는가

`monitoring` ns 의 Alertmanager 는 Helm(플랫폼) 소관이다. values 나
`alertmanager-prom` Secret 을 직접 고치면 소관 침범이고 다음 helm upgrade 에서 원복된다.

대신 CR 이 확장 지점을 열어두고 있다(2026-07-27 실측):

```
alertmanagerConfigSelector          = {}   # 모든 AlertmanagerConfig 수집
alertmanagerConfigNamespaceSelector = {}   # 모든 namespace
ruleSelector                        = {"matchLabels":{"release":"prom"}}
ruleNamespaceSelector               = {}   # 모든 namespace
```

→ **우리 namespace(llmops)에 리소스를 두기만 하면** operator 가 수집·병합한다.
플랫폼 파일은 그대로다.

## 남은 결정 사항 (PL/플랫폼)

**수신처가 확정되지 않아 적용하지 않았다.** 클러스터에 사용 가능한 알림 자격증명이 없다.

- Slack incoming webhook URL — 기존 자격증명 0건
- SMTP 릴레이 — 미설정(차트 기본값)
- 사내 webhook 엔드포인트 — 있으면 가장 간단
- `llmops-notify-api`(GenOS) — ConfigMap 상 DB/MQ/Redis 만 참조. 외부 채널 여부·API 계약 미확인

확정되면 `alertmanagerconfig-jw-market.yaml` 의 `url` 을 교체하고 적용한다.
자격증명이 필요하면 **llmops namespace 에** Secret 을 먼저 만들어야 한다
(AlertmanagerConfig 는 같은 namespace 의 Secret 만 참조 가능).

## 적용 후 검증

```sh
kubectl exec -n monitoring alertmanager-prom-0 -c alertmanager -- \
  amtool config routes test \
  --config.file=/etc/alertmanager/config_out/alertmanager.env.yaml \
  alertname=KubeJobFailed namespace=llmops severity=warning
# "null" 이 아니라 jw-market-notify 가 나와야 한다
```

병합 예상 설정에 대한 사전 검증은 2026-07-27 에 완료했다 —
llmops 알림은 새 수신처로, cicd/gpu-operator 알림은 기존대로 `null` 로 가는 것을 확인했다.
