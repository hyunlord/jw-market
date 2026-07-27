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

**★ 범위(scope)는 두 안 중 택일이며 PL 판단 사항이다. 같은 역할의 파일을 동시에 적용하지 말 것**
(각 쌍은 `metadata.name` 이 동일하다).

| 파일 | 안 | 상태 | 설명 |
|---|---|---|---|
| `alertmanagerconfig-jw-market.yaml` | 안1 | **미적용 · PLACEHOLDER** | llmops **namespace 전체**. 단순하지만 남의 알림 10건이 섞인다 |
| `alertmanagerconfig-jw-market-scoped.yaml` | 안2 | **미적용 · PLACEHOLDER** | 워크로드 식별 라벨 정규식으로 **우리 것만**. 새 워크로드 추가 시 정규식 갱신 필요 |
| `prometheusrule-jw-market-oom.yaml` | 안1 | **미적용** | llmops 전체 OOM. 24h 기준 5건(chat-agent 4 포함) |
| `prometheusrule-jw-market-oom-scoped.yaml` | 안2 | **미적용** | 우리 워크로드만. 24h 기준 1건 |

### 범위 문제 (2026-07-27 정정)

`llmops` 는 **공유 namespace** 다. 우리 것 외에 chat-agent · litellm · vertex-proxy ·
code-serving · preprocessor · workflow · mcp-*(GenOS)가 함께 들어 있다.
따라서 `namespace="llmops"` 단일 매처는 "우리 알림만"이 아니다.

실측 — llmops 발화 **29건**의 소관 분포:

| 소관 | 건수 |
|---|---|
| jw market | **18** |
| platform/GenOS | 6 |
| jw agent | 3 |
| jw chat | 1 |
| [미확인] | 1 |

**단일 매처로 우리 것만 고를 수 없는 이유**: kube-prometheus-stack 알림은
워크로드 식별 라벨이 알림마다 다르다 — `KubeJobFailed`=`job_name`,
`KubeDeploymentReplicasMismatch`=`deployment`, `KubeHpaMaxedOut`=`horizontalpodautoscaler`,
`KubePodNotReady`/`KubeContainerWaiting`=`pod`. **owner/team 공통 라벨은 없다**
(`service` 라벨은 `prom-kube-state-metrics` = 지표 수집원이지 워크로드가 아니다).
게다가 KSM 원천 알림은 `pod` 라벨이 **exporter 파드**라 pod 기준 필터로는 안 잡힌다.
→ 안2 는 라벨별 서브라우트 4개로 구성했다.

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
