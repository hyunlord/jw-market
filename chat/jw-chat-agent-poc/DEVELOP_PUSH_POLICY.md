# chat develop-push 정책 (2026-07-18 규칙 전환, PL 승인)

## 배경
과거 규칙은 **"chat 작업분 develop push 전면 금지"**였다. 의도는 chat 작업이 backend/pipeline
의 `develop` 을 흔들지 않게 하는 것이었다. 그러나 chat 코드가 `chat/` 최상위로 격리(`0900ed5e`)된
뒤 실측한 결과, chat 과 backend/pipeline 의 변경 경로가 **배타적으로 분리**됨이 증명됐다
(merge-base 이후 develop 은 `chat/` 를 0건 변경, 피처는 `chat/` 밖을 0건 변경 → 교집합 ∅,
merge-tree 양방향 충돌 0). 전면 금지는 그 결과 하루 30~80 커밋씩 재분기(일주일 723 커밋)와
운영 이미지가 정본 계보에서 벗어나는 부채를 낳았다.

## 신규 규칙 (이후 상시)
1. **chat 작업분은 `chat/` 경로에 한해 `develop` 에 직접 정본화**한다(`safe_push`).
2. **push 전 필수 검사**: `chat/` 밖 변경 0. 한 줄이라도 있으면 **중단·조율**
   (`git diff --name-only origin/develop HEAD -- ':!chat/'` 가 비어야 함).
3. **`safe_push`**: fast-forward only · force 금지 · push 후 **원격 SHA == 로컬** 확인(hard stop).
4. **대규모·고위험 작업**은 피처 브랜치에서 진행하되 **완료 즉시 `develop` 반영**한다(장기 보유 금지 —
   장기 분기가 이번 부채의 원인).
5. **`docs/delivery/` 문서 예외**는 기존대로 유지(문서 정본화 목적의 develop 반영 허용).
6. **backend/pipeline 세션과 경로 충돌 시** 즉시 중단·조율한다. `chat/` 밖은 chat 세션 소관이 아니다.

## 운영 계보 고정 (재현성 R축)
운영에 배포한 chat 이미지의 소스 커밋에는 **`ops/chat-<빌드id>-<커밋>-<날짜>` 형식의 태그**를
부여해 "이 시점 운영 = 이 커밋"을 영구 고정한다(예: `ops/chat-838-da3fc153-20260718` → `da3fc153`).
이렇게 하면 `develop` 이 전진해도 운영 이미지의 재현 추적이 끊기지 않는다.

## 목적
723 커밋급 재분기 방지 · 운영 이미지가 정본(`develop`) 계보에서 나오게 함 · SI 납품 문서의
계보 이중 기준(develop 인용 + [운영 이미지 기준]) 해소.

> 근거·타당성 실측: `docs/delivery/evidence/chat_lineage_gap.md`, 머지 타당성 진단
> (`/tmp/chat_merge_feasibility_20260718`). 최초 정본화 머지 = develop `dd8a3919`.
