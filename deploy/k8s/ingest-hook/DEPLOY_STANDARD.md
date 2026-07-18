# 인입 스택 배포 표준 — 이미지 참조 digest 정합 (2026-07-19)

jw-ingest-hook / jw-ingest-sweep-daily 배포 시 **이미지 참조 드리프트**를 막는 표준.
근거: image reference digest alignment audit (2026-07-19).

## (a) 관측된 드리프트
- 가변 태그가 두 digest 를 가리킨 이력: `:v0.2.4-e984a057` → 레지스트리 `e79aa098` vs 구동 pod `fea29685`.
- 정합 전 라이브: hook = 가변 태그 `:v0.2.5-51e2c687`, sweep = 가변 태그 `:v0.2.4-e984a057`(J5 이전 빌드).
- 정합 후: 둘 다 불변 digest `@sha256:a362ceb8…`(v0.2.5, J5 포함).

## (b) 표준: 불변 digest 만
- 이미지 참조는 **불변 digest(`@sha256:…`)만**. 가변 태그 경유 배포 금지.
- 재빌드 절차: 새 digest 확보 → manifest 갱신·커밋 → apply. `kubectl set image` 에 **태그** 지정 금지(digest 는 허용).
- ★ orchestrator 재빌드 시 **digest 통지 + manifest digest 갱신을 세트로** 처리(둘 중 하나만 하면 드리프트 재발 — jw agent 합의사항).

## (c) 배포 후 게이트 (1줄)
```
kubectl get deploy/jw-ingest-hook       -n llmops -o jsonpath='{..image}'   # @sha256 형식이어야 함
kubectl get cronjob/jw-ingest-sweep-daily -n llmops -o jsonpath='{..image}' # @sha256 형식이어야 함
```

## ★★ inert/무장 경고 — 이 저장소 manifest 를 그대로 apply 하지 말 것
- `ingest-trigger-deployment.yaml` 은 **의도적으로 `replicas: 0`(inert 등록)**. 무장(replicas 1)은 PL 게이트 하의 운영 조치이며 이 manifest 의 기본값이 아니다.
- **이 develop manifest 를 그대로 `kubectl apply` 하면 `replicas 0` 으로 파일럿이 disarm 된다.** (D-3a 무장 파괴)
- 라이브 무장 상태(`replicas 1` + digest + `INGEST_LOAD_STAGING_ROOT`)의 apply-source 는 별도다 — 2026-07-19 실측 `last-applied-configuration = replicas 1 + @a362ceb8`.
- **이미지만 정합**할 때는 `kubectl set image <res> <container>=…@sha256:<digest>` (replicas·env 무접촉) 를 쓴다. 본 라운드도 이 방식으로 무장 보존.

## sweep env 잔여 (2026-07-19 · 문자 그대로 W-3 로 digest 만 정합)
- 라이브 sweep 은 여전히 `INGEST_REHEARSAL_ROOT=/tmp/ingest-rehearsal` (git manifest 는 `INGEST_LOAD_STAGING_ROOT`). 본 라운드는 digest 만 v0.2.5 로 정합했다.
- ★ **D-3a 원복 시 sweep resume 전에 env 를 staging 으로 정합**해야 J5 실모드로 돈다. 현재 상태로 resume 하면 리허설 모드(CSV→sqlite)로 돈다.
