# DOC-4b · JW 데이터 인입 포털(jw-data-input) 사용설명서

| 항목 | 값 |
|---|---|
| 문서 버전 | v1.0 |
| 사이트 정본 | Gitea `jw-market/jw-data-input.git` · HEAD(feat/market-ingest-v21) `8ca9d987` |
| 근거 코드 | `/tmp/site-head/web/src` (Next.js — app/·components/·lib/) — **배포 이미지와 동일 커밋** |
| 운영 배포 | GKE `llmops` ns · deployment `jw-data-portal`(+worker) (`jw-data-portal:v0.6.0-8ca9d98`) |
| 접속 URL | `https://jwai-dev.jwhealthcare.com/jw-data-portal/` |
| 생성일 | 2026-07-17 (v0.6.0 재배포 반영) |

> **본 문서의 범위.** JW 데이터 인입 포털에 데이터를 올리는 담당자(사용자)를 위한 안내서다. 모든 화면 문구·버튼 라벨·상태값은 사이트 실코드(`/tmp/site-head/web/src`)에서 확인한 실제 문자열이다. **본 문서의 근거 코드는 현재 배포 이미지 `v0.6.0-8ca9d98`과 동일 커밋(Gitea HEAD `8ca9d987`)이다.** 확인 불가 항목은 **[확인 필요]**로 표시했다.
>
> **★ 현행 배포(v0.6.0) 기준 활성 구분.** 배포 환경변수(`dataportal_env_v060.txt`)는 `STORAGE_PROVIDER=local`을 유지하되, **MinIO 접속 정보**(`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_MARKET_BUCKET`)와 **인입 훅 URL**(`INGEST_HOOK_TRIGGER_URL`, `INGEST_HOOK_STATUS_URL`, `INGEST_HOOK_TIMEOUT_MS=5000`)이 **추가로 설정**되었다. 핵심은 **스토리지 라우팅이 에이전트 단위**라는 점이다: 시장(market) 에이전트는 설정 자체가 `storage.provider="s3"`(`config/agents.ts` 아래)이므로 `STORAGE_PROVIDER=local`과 **무관하게 항상 MinIO(S3) 경로**를 탄다. 따라서 v0.6.0에서는 **R&D 업로드(NFS)와 시장 데이터 인입(MinIO 제출 확정 → 훅 트리거 → 상태 조회) 플로우가 모두 활성**이다.
>
> **단, 백엔드 인입 훅(`jw-ingest-hook:8080`)은 리허설 격리 모드로 운영된다(운영/PL 통지 사항).** 즉 포털 측 플로우(업로드·제출 확정·manifest 기록·훅 트리거·상태/재실행)는 실동작하지만, 그 뒤 실제 마트 적재는 격리된 리허설로 처리되며 본운영 적재로의 전환은 별도 게이트다. 아래 각 절에 **[현행 활성]** / **[활성 · 리허설 격리]** 를 명기한다.

---

## 1. 로그인 / 접속

- 접속 주소: `https://jwai-dev.jwhealthcare.com/jw-data-portal/` (인증 base `NEXTAUTH_URL=.../jw-data-portal/api/auth`, BASELINE `dataportal_env.txt`).
- 인증 방식: **NextAuth 기반 Google 계정 로그인.** 로그인 버튼 문구는 **"Google 계정으로 로그인"** 이며, 로그인 진행 중에는 "로그인 준비 중..."으로 바뀐다(`components/LoginButton.tsx:11,44`).
- 로그인하지 않은 상태로 포털/업로드 화면에 접근하면 자동으로 `/login`으로 이동한다(`app/page.tsx:37-39`, `app/(portal)/market/page.tsx:12-14`, `app/(portal)/rnd/page.tsx:12-14`).
- 접근 권한이 없는 계정은 미인가 화면으로 처리된다(`app/unauthorized/page.tsx`, `middleware.ts`). 홈 안내 문구: **"허용된 사용자만 데이터를 업로드할 수 있습니다."**(`app/page.tsx:64-66`).

`[화면: 로그인 페이지 — "Google 계정으로 로그인" 버튼]`

---

## 2. 포털 홈 — 두 개의 업로드 경로

로그인하면 홈(`JW 데이터 인입 포털`, `app/page.tsx:61-63`)에 **에이전트 2개**가 카드로 표시된다(`app/page.tsx:80-81` "에이전트 2", `config/agents.ts`의 `rnd`/`market`).

| 카드 | 경로 | 용도 | 현행 상태 |
|---|---|---|---|
| **RND** | `/rnd` | R&D 문서 업로드(논문·프로젝트) | **[현행 활성]** |
| **MARKET** | `/market` | 시장분석 데이터 인입(UBIST·IQVIA) | **[활성 · 리허설 격리]** |

- 각 카드를 누르면 해당 업로드 화면으로 이동한다(`app/page.tsx:108-111`, `href={/${agent.id}}`).
- 홈에는 "업로드 이력 대시보드"(`/dashboard`)와 저장 경로 미리보기가 함께 표시된다(`app/page.tsx:179-207`).
- 관리자 계정에는 "관리자 페이지"(`/admin`) 진입 영역이 추가로 표시된다(`app/page.tsx:209-229`).

`[화면: 홈 — RND / MARKET 두 에이전트 카드]`

> R&D 화면(`/rnd`)과 시장(`/market`) 화면은 **동일한 업로드 컴포넌트**(`UploadPage`)를 사용한다(`app/(portal)/rnd/page.tsx:24`, `app/(portal)/market/page.tsx:24`). 차이는 카테고리 구성과, 시장 카테고리에만 존재하는 **제출 확정(인입) 단계**다(4장).

---

## 3. 업로드 공통 절차 (Step 1 → Step 2)

두 경로 모두 아래 순서를 따른다(`components/UploadPage.tsx`).

### Step 1 · 카테고리 선택 (`UploadPage.tsx:1085-1206`)

- 헤더 문구: **"Step 1 · 카테고리 선택"**. "카테고리를 선택하면 파일 목록과 업로드 큐가 해당 분류 기준으로 초기화됩니다."
- 카테고리 검색창("카테고리 검색...")으로 필터링할 수 있고, 표시 개수/최근 사용 카테고리가 배지로 표시된다.
- 각 카테고리 카드에는 허용 확장자와 "최대 NMB"(파일 크기 상한)가 표시된다(`UploadPage.tsx:1192-1197`).

**R&D 카테고리** (`config/agents.ts:143-177`): "논문"(papers), "프로젝트"(projects). 저장 경로 `rnd_docs`/`rnd_proj`.

**시장 카테고리** (`config/agents.ts:178-250`): "UBIST - Sales", "UBIST - Weekly", "IQVIA - NSA", "IQVIA - CHSO", "IQVIA - CSD - ChannelDynamics", "IQVIA - CSD - keyword", "IQVIA - CSD - meetings", "sell in & sell out" 등. 이 중 인입(`ingest`) 대상 카테고리는 제출 확정 단계를 갖는다(4장).

### Step 2 · 파일 업로드 (`UploadPage.tsx:1279-1404`)

- 헤더 문구: **"Step 2 · 파일 업로드"**, "선택된 분류: {카테고리}".
- 파일 추가: **"파일 또는 폴더를 여기에 끌어다 놓으세요"** 드래그앤드롭, 또는 **"파일 선택"** / **"폴더 선택"** 버튼(`UploadPage.tsx:1320,1336,1343`).
- 허용 형식·최대 크기 안내가 표시되며, 폴더 업로드 시 허용 확장자 외 파일은 자동 제외된다(`upload-helpers.ts`의 `filterFilesByAcceptedExtension`).
- 추가 검증 옵션(체크박스): **"파일 시그니처 검증"**, **"MIME 타입 검증"** (서버 업로드 시 추가 검사, `UploadPage.tsx:1374-1401`).
- 업로드 시작 버튼: **"{N}개 파일 업로드"** (전송 중에는 "업로드 중... (성공+실패/전체)")(`UploadPage.tsx:1446-1455`).
- 업로드 큐는 동시 3개·최대 3회 재시도로 처리된다(`UploadPage.tsx:472-474`, `UploadQueue`).

**파일별 상태 표시**(`UploadPage.tsx:130-143`): 대기 / 준비 / 전송 / 완료 / 오류. 오류 파일은 행에서 **"재시도"**, 전송 중 파일은 **"취소"** 할 수 있다(`UploadPage.tsx:285-300`).

**진행 중 제어**: 일시정지 / 재개 / 전체 취소 / (실패 시)"실패 파일 재시도" / 전체 삭제(`UploadPage.tsx:1247-1273,1471-1487`).

- **저장 경로(에이전트 단위 라우팅):**
  - **R&D**: `UPLOAD_BASE_PATH=/nfs-root/autoIngestion` 하위 NFS에 에이전트·그룹·카테고리·날짜·파일명 세그먼트로 저장된다(`app/page.tsx:187-190`; R&D 에이전트 `storage.provider="local"`).
  - **시장(market)**: 시장 에이전트는 `storage.provider="s3"`이므로 업로드가 **MinIO 버킷**(`MINIO_MARKET_BUCKET`)으로 향한다(`config/agents.ts`, `lib/storage.ts:910-914`). v0.6.0에서 `MINIO_*` env가 설정되어 실동작한다.

`[화면: Step 1 카테고리 선택 목록]`
`[화면: Step 2 파일 업로드 — 드래그앤드롭 및 파일/폴더 선택]`
`[화면: 업로드 진행 상태바 및 파일 목록(대기/전송/완료/실패)]`

---

## 4. 시장분석 데이터 인입 (Step 3 → 상태 → 재실행) — [활성 · 리허설 격리]

시장 카테고리 중 인입 대상(`ingest`)은 업로드 성공 후 **제출 확정(세트 완결)** 단계를 거쳐 자동적재로 넘어간다. 인입 대상 카테고리와 데이터 기간(epoch) 단위는 다음과 같다(`config/agents.ts:178-247`).

| 카테고리 | 인입 카테고리 | epoch 단위 |
|---|---|---|
| UBIST - Sales | ubist | month(월) |
| UBIST - Weekly | ubist | week(주) |
| IQVIA - NSA | iqvia | quarter(분기) |
| IQVIA - CHSO | iqvia | quarter(분기) |
| IQVIA - CSD - ChannelDynamics | iqvia | quarter(분기) |
| IQVIA - CSD - keyword | iqvia | quarter(분기) |
| IQVIA - CSD - meetings | iqvia | quarter(분기) |

### 4.1 epoch(데이터 기간)이란

- epoch는 제출하는 데이터의 **대상 기간**이며, 카테고리별로 월/분기/주 단위다(`lib/market-ingestion.ts:4` `MarketEpochKind = "month" | "quarter" | "week"`).
- 형식(`lib/market-ingestion.ts:23-27`): 월 `YYYY-MM`(예 `2026-06`), 분기 `YYYY-QN`(예 `2026-Q2`), 주 `YYYY-WNN`(예 `2026-W27`).
- 파일을 추가하면 파일명에서 기간을 **자동 추론**해 채운다(`deriveMarketEpoch`, `UploadPage.tsx:608-615`). 값은 확정 화면에서 직접 수정할 수 있다.

### 4.2 Step 3 · 제출 확정 (`UploadPage.tsx:1004-1033`, `852-872`)

인입 대상 카테고리에서 파일이 **모두 성공(실패 0)** 하면 확정 안내가 뜬다(`UploadPage.tsx:984-988`).

- 헤더: **"Step 3 · 세트 완결"**, 제목 **"시장분석 데이터 제출 확정"**.
- 안내: "확정 전에는 manifest가 기록되지 않습니다. 아래 기간과 파일 해시를 확인한 뒤 확정하세요."
- 입력 항목: **"데이터 기간"**(epoch) — placeholder는 단위에 따라 `2026-06`/`2026-Q2`/`2026-W27`.
- 파일별 `sha256` 해시가 목록으로 표시된다(`UploadPage.tsx:1019-1026`).
- 버튼: **"제출 확정"**(확정 중에는 "확정 중...") / **"나중에 확정"**(`UploadPage.tsx:1028-1029`).
- 확정 성공 시 토스트: 인입 훅이 실제 호출되면 "제출 확정 완료 · 적재가 시작됩니다", 훅이 mock/대기 상태면 "제출 확정 완료 · 적재 트리거 대기"(`UploadPage.tsx:866`). **v0.6.0에서는 `INGEST_HOOK_TRIGGER_URL`이 설정되어 실제 훅이 호출된다**(아래 근거 참조).

**확정 API:** `POST /api/market/submissions/{setId}/confirm`, 본문 `{ epoch }`(`UploadPage.tsx:859-862`, `app/api/market/submissions/[setId]/confirm/route.ts`). 확정 시 서버는 manifest를 MinIO 버킷에 기록(`putObject`)하고 인입 훅을 트리거한다(`confirm/route.ts:29-32`).

> **★ 스토리지·훅 게이트 판정 (v0.6.0 실동작 기준).** 제출 확정은 **시장 에이전트용 MinIO(S3) 프로바이더**로 처리된다.
> - **에이전트 단위 라우팅:** 스토리지 선택은 `agent.storage.provider ?? STORAGE_PROVIDER ?? "local"` 순서다(`lib/storage.ts:910`). 시장 에이전트는 `config/agents.ts`에서 `storage: { provider: "s3", bucketEnv: "MINIO_MARKET_BUCKET" }`로 지정되어 있어, **`STORAGE_PROVIDER=local`과 무관하게 항상 S3 경로**를 탄다. 따라서 confirm 라우트의 `if (!(storage instanceof S3StorageProvider))` 검사(`confirm/route.ts:25-27`, 오류 문구 "시장분석 MinIO 설정이 필요합니다")는 시장 에이전트에서는 **통과**한다(이 분기는 사실상 발동하지 않음).
> - **실제 게이트 = MinIO 접속 env.** `S3StorageProvider` 생성자는 `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`를 필수로 읽고(`storage.ts:558-566` `requireEnv`), 버킷은 `MINIO_MARKET_BUCKET`에서 해석한다(`storage.ts:281-303`, `resolveS3Bucket`). 이 값이 없으면 생성자가 예외를 던져 확정이 실패(500)한다. **v0.6.0 배포에는 이 4개 값이 모두 설정**되어 있어 프로바이더가 정상 생성되고 확정이 진행된다.
> - **인입 훅 = 실호출.** `INGEST_HOOK_TRIGGER_URL`이 없으면 훅은 mock 처리되어 "대기(waiting)"로만 남지만(`lib/ingest-hook-client.ts:16-17`), **v0.6.0에는 트리거·상태 URL이 설정**되어 실제 훅(`jw-ingest-hook:8080`)이 호출된다.
> - **→ 판정:** 포털 측 인입 플로우(업로드 → 제출 확정 → manifest MinIO 기록 → 훅 트리거 → 상태 조회 → 재실행)는 **v0.6.0에서 실동작한다.** 다만 백엔드 훅은 **리허설 격리 모드**로 운영되므로(운영/PL 통지), 훅이 반환·표시하는 상태는 리허설 처리 결과이며 본운영 마트 적재로의 전환은 별도 게이트다.

### 4.3 적재 상태 확인 (`components/MarketSubmissionStatusPanel.tsx`)

제출한 세트의 처리 상태는 상태 패널에서 확인한다.

- 패널 제목: **"시장분석 세트 적재 상태"**, "제출 확정된 manifest의 훅 처리 상태를 표시합니다."(`MarketSubmissionStatusPanel.tsx:80-85`).
- 새로고침 버튼: **"상태 새로고침"**(조회 중 "조회 중...")(`MarketSubmissionStatusPanel.tsx:93`).
- 목록 API: `GET /api/market/submissions`(`MarketSubmissionStatusPanel.tsx:25`).

**상태값**(`lib/market-status-display.ts:3-32`):

| 상태 라벨 | 내부값 | 재실행 가능 |
|---|---|---|
| 미확정 | draft | 아니오 |
| 대기 | waiting | 예 |
| 적재중 | running | 아니오 |
| 완료 | completed | 아니오 |
| 실패 | failed | 예 |

- 인입 훅 상태 조회는 `INGEST_HOOK_STATUS_URL`이 설정된 경우에만 실제 조회하며, 없으면 "대기(waiting)"로 표시된다(`lib/ingest-hook-client.ts:28-30`). **v0.6.0에는 이 URL이 설정**되어 실제 상태를 조회한다. 훅의 `queued`/`complete` 등 원천 상태는 위 표의 대기/완료로 매핑된다(`ingest-hook-client.ts:3-6`). 조회 타임아웃은 `INGEST_HOOK_TIMEOUT_MS=5000`이다.

### 4.4 재실행 (retry) (`MarketSubmissionStatusPanel.tsx:48-74`)

- **대기·실패** 상태의 세트는 수동 재실행할 수 있다(`market-status-display.ts` `retryable` 플래그).
- 버튼: **"이 세트 수동 재실행"**(요청 중 "요청 중...")(`MarketSubmissionStatusPanel.tsx:162-164`).
- 재실행 API: `POST /api/market/submissions/{setId}/retry`(`MarketSubmissionStatusPanel.tsx:52-55`).

`[화면: Step 3 시장분석 데이터 제출 확정 모달 (데이터 기간·파일 해시)]`
`[화면: 시장분석 세트 적재 상태 패널 (상태 배지·수동 재실행)]`

---

## 5. 업로드 이력 대시보드

- 홈 또는 업로드 화면 하단의 "업로드 이력 보기"/"업로드 이력 대시보드"에서 `/dashboard`로 이동한다(`app/page.tsx:193-206`, `UploadPage.tsx:1501-1502`).
- 대시보드는 업로드된 파일 목록과 저장 경로를 최신순으로 표시한다(`app/page.tsx:200-201`, `app/(portal)/dashboard/page.tsx`).

`[화면: 업로드 이력 대시보드]`

---

## 6. 요약 — v0.6.0 활성 상태

| 기능 | 상태 | 근거 |
|---|---|---|
| Google 로그인 / 접근 통제 | **활성** | `LoginButton.tsx`, `middleware.ts` |
| R&D 문서 업로드(NFS `/nfs-root/autoIngestion`) | **활성** | 시장 외 에이전트는 `STORAGE_PROVIDER=local`, `UPLOAD_BASE_PATH` |
| 카테고리 선택·파일/폴더 업로드·검증 옵션 | **활성** | `UploadPage.tsx` |
| 시장 데이터 제출 확정(MinIO manifest) | **활성** | 시장 에이전트 `storage.provider="s3"`(`config/agents.ts`)+`MINIO_*` env 설정, `confirm/route.ts:29-32` |
| 인입 트리거/상태 훅 | **활성 · 리허설 격리** | `INGEST_HOOK_TRIGGER_URL`/`STATUS_URL` 설정, 백엔드 `jw-ingest-hook:8080`(리허설 격리 모드) |
| 세트 적재 상태·수동 재실행 | **활성** | `MarketSubmissionStatusPanel.tsx`, 상태 URL 실조회 |
| 본운영 마트 적재 전환 | **별도 게이트** | 백엔드 훅 리허설 격리(운영/PL) |

---

## 7. 화면 캡처 플레이스홀더 (캡처 리스트)

1. `[화면: 로그인 페이지 — "Google 계정으로 로그인" 버튼]`
2. `[화면: 홈 — RND / MARKET 두 에이전트 카드]`
3. `[화면: Step 1 카테고리 선택 목록]`
4. `[화면: Step 2 파일 업로드 — 드래그앤드롭 및 파일/폴더 선택]`
5. `[화면: 업로드 진행 상태바 및 파일 목록(대기/전송/완료/실패)]`
6. `[화면: Step 3 시장분석 데이터 제출 확정 모달 (데이터 기간·파일 해시)]`
7. `[화면: 시장분석 세트 적재 상태 패널 (상태 배지·수동 재실행)]`
8. `[화면: 업로드 이력 대시보드]`

**플레이스홀더 총 8개.**

---

## 부록 · 확인 필요 항목 요약

| # | 항목 | 사유 | 처리 |
|---|---|---|---|
| 1 | 미인가 화면(`unauthorized`) 실제 문구 | 본 문서에서 파일 존재만 확인, 정확한 안내 문구 미인용 | ✅ 해소(2026-07-18, `web/src/app/unauthorized/page.tsx:27-73` 실측) |

**미인가 화면 실제 문구**(사이트 정본 HEAD `8ca9d987`, `web/src/app/unauthorized/page.tsx`):
- 상단 라벨(영문): **"Access Restricted"**
- 제목: **"접근 권한이 없습니다"**
- 본문: **"현재 로그인한 계정은 이 포털에 허용되지 않았거나, 요청한 화면에 대한 권한이 없습니다."**
- 정보 카드: **로그인 계정**(email)·**현재 역할**(role)·**요청 경로**(requestPath) — 미상 시 폴백 "알 수 없는 계정"/"없음"/"알 수 없음".
- 안내·CTA: **"관리자에게 접근 권한을 요청한 뒤 다시 로그인하세요."** + 버튼 "다른 계정으로 로그인".

**[확인 필요] 총 0건**(1건 해소).
