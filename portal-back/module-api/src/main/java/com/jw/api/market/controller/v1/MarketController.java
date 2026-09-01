package com.jw.api.market.controller.v1;

import com.jw.core.base.response.Response;
import com.jw.service.market.dto.v1.Market;
import com.jw.service.market.service.v1.MarketService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@Tag(name = "MARKET Analysis API - v1", description = "MARKET Analysis 구성 API v1")
@RestController("MarketControllerV1")
@RequestMapping("/api/v1/market")
public class MarketController {

    private final MarketService marketService;

    public MarketController(
            @Qualifier("MarketServiceV1") MarketService marketService
    ) {
        this.marketService = marketService;
    }

    @Operation(
        summary = "Market 제품 조회",
        description = "JW brand 25개 + 검색 / 드롭다운 / 외부 조회용"
    )
    @PostMapping("/brands")
    public ResponseEntity<Response> brands(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Brand marketRequest
    ) { return ResponseEntity.ok(marketService.getBrands(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 제품 상세 조회",
        description = "JW 주요 brand 25개의 카드 평탄 리스트. 1:1 매핑."
    )
    @PostMapping("/status")
    public ResponseEntity<Response> status(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Status marketRequest
    ) { return ResponseEntity.ok(marketService.getStatus(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 심층분석 조회",
        description = "시계열 예측 (history+forecast+CI) + 경쟁사 forecast + 이벤트(매칭) + 5개 UBIST 월간 시계열 + AI 인사이트."
    )
    @PostMapping("/analysis")
    public ResponseEntity<Response> analysis(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Analysis marketRequest
    ) { return ResponseEntity.ok(marketService.getAnalysis(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 동적 시장 원인분석 재계산",
        description =
            "`/api/dynamic-market` 는 포탈 원인분석 payload를 **캐시 없이 재계산**하는 POST API입니다. 응답은 항상 `status` / `result` envelope이며, `result` 는 `/api/cause/{brand}` 가 돌려주는 root 구조(`brand`, `market_id`, `market_meta`, `data`)와 같은 모양입니다.\n" +
            "\n" +
            "### 요청 body 최상위 필드\n" +
            "\n" +
            "| 필드 | 타입 | 필수 | 기본값 | missing 처리 | null 처리 |\n" +
            "| --- | --- | --- | --- | --- | --- |\n" +
            "| `view` | string | 아니오 | `null` | legacy `view_kind` 기반 추론 | legacy `view_kind` 기반 추론 |\n" +
            "| `source` | string | 아니오 | `ubist` | `ubist` 로 계산 | 422 validation error |\n" +
            "| `measure` | string | 아니오 | `sales` | `sales` 로 계산 | 422 validation error |\n" +
            "| `filters` | object | 아니오 | 빈 필터 객체 | 빈 필터 객체 | 422 validation error |\n" +
            "| `options` | object | 아니오 | `{period_range:null}` | 기본 옵션 객체 | 422 validation error |\n" +
            "\n" +
            "- `source`: `ubist`, `iqvia`, `iqvia_nsa`, `nsa` 허용. 내부에서 `iqvia` / `nsa` 는 `iqvia_nsa` 로 정규화됩니다.\n" +
            "- `measure`: UBIST는 `sales`, `volume` / IQVIA는 `sales`, `unit`, `counting_unit`, `dosage_unit` 만 유효합니다.\n" +
            "\n" +
            "### 시장 범위 결정 (filters.atc4)\n" +
            "- 빈 `analysis_level` 차원 = 그 차원을 적용하지 않는 **전체 선택(select-all)**.\n" +
            "- top-level `filters.atc4` 는 일반뷰 · 전략뷰 공통 시장 범위입니다.\n" +
            "- **일반뷰**: `filters.atc4` 생략 + `focus_brand_key` 전송 → 해당 브랜드가 속한 모든 ATC4 합집합.\n" +
            "- **전략뷰**: `filters.atc4` 생략/빈 배열 → 선택된 전략 시장 전체.\n" +
            "- 일반뷰에서 `focus_brand_key` 와 `filters.atc4` 가 모두 없으면 범위를 정할 수 없어 **400**.\n" +
            "\n" +
            "### 공통 filters 필드\n" +
            "\n" +
            "| 필드 | 타입 | 기본값 | 동작 |\n" +
            "| --- | --- | --- | --- |\n" +
            "| `atc4` | `string[]` | `[]` | 일반뷰 = 시장 scope, 전략뷰 = ML/CD 시장 안의 ATC narrowing. 생략/빈 배열은 select-all. |\n" +
            "| `view_kind` | string / null | `null` | `market_landscape`·`strategic_ml`·`ml` = ML 전략뷰, `competitive_dynamics`·`strategic_cd`·`cd` = CD 전략뷰. 값이 있으면 전략뷰 분기. |\n" +
            "| `focus_brand_key` | string / null | `null` | `filters.atc4` 생략 시 브랜드 기준 ATC4 합집합 생성에 사용. 빈 문자열은 대부분 미입력처럼 처리. |\n" +
            "| `analysis_level` | object | 빈 source 객체 | 소스별 필터 딕셔너리. row filter와 값 슬라이스를 같은 source 하위에 넣습니다. |\n" +
            "\n" +
            "> `filters` 생략 = 빈 객체. `filters:null` 은 허용되지 않습니다. 중첩 list 필드는 생략 시 `[]`, `null` 이면 422, 빈 list이면 미적용. 선택 string 필드(`view_kind`, `focus_brand_key`)는 missing/null 모두 None이며, 빈 문자열은 미입력 또는 잘못된 id로 처리될 수 있어 **보내지 않는 것을 권장**합니다.\n" +
            "\n" +
            "### 일반뷰 `analysis_level.ubist` 허용 키\n" +
            "`atc3`(ATC3 좁히기), `atc4`(ATC4 좁히기), `seller`(판매사), `molecule`(성분), `molecule_strength`(성분용량), `form`(제형), `route`(투여경로), `reimbursement`(급여구분), `facility`(종별), `specialty`(진료과), `pairs`(종별×진료과 pair)\n" +
            "\n" +
            "### 일반뷰 `analysis_level.iqvia` 허용 키\n" +
            "`mfr_name_kor`(제조사명), `molecule_type`(성분구분), `molecule_desc`(성분명), `pack_desc`(PACK DESC), `strength`(함량), `nhi_type`(NHI 구분), `audit_code`(IQVIA audit code)\n" +
            "- 모든 IQVIA 분석레벨 필터는 같은 `analysis_level.iqvia` 객체에서 함께 보냅니다.\n" +
            "- `pack_desc` 의 내부 canonical dimension_type은 `pack` 입니다.\n" +
            "- `audit_code` 는 row filter가 아니라 raw `audit_code_matrix` **값 슬라이스**입니다. missing/빈 배열이면 전체 audit code 포함.\n" +
            "\n" +
            "### 전략뷰 필터\n" +
            "- 전략뷰도 top-level `filters.atc4` 하나로 ATC narrowing.\n" +
            "- `analysis_level.<source>.atc3` / `.atc4` 는 전략뷰 narrowing 입력이 **아니며** active 값이 있으면 **400**.\n" +
            "- `class`, `mfr`/`mfr_name_kor`, `nhi`/`nhi_type`, `molecule`·`pack`·`strength`·`form`·`route`·`reimbursement` 계열도 전략뷰 요청 필터가 아닙니다.\n" +
            "- `facility`, `specialty`, `pairs`, `audit_code` 값 슬라이스는 일반뷰 전용 → 전략뷰에서 active 값이 있으면 **400**.\n" +
            "- 전략 시장 id(`ml_id`, `cd_market_id`)는 공개 요청 필드가 **아닙니다**. `focus_brand_key` + `view_kind` 만 보내면 백엔드가 브랜드가 속한 ML/CD 시장을 조회합니다.\n" +
            "- 브랜드가 여러 시장에 속하면 **시장 id 오름차순 첫 번째**를 결정론적으로 사용 (예: `ml_005`/`ml_008` → `ml_005`, `cd_006`/`cd_007` → `cd_006`).\n" +
            "- `ml_id` / `cd_market_id` 를 요청에 포함하면 schema extra-forbid로 **422**.\n" +
            "- 다른 source 객체에 값이 있으면 **400** (예: `source:\"iqvia\"` 에서 `analysis_level.ubist.seller` 값 → `analysis_level must match selected source`).\n" +
            "\n" +
            "### options\n" +
            "- `period_range.start` / `end`: 선택 기간 경계. 생략 / `null` / `{}` 는 전체 기간.\n" +
            "\n" +
            "### 응답 구조\n" +
            "성공 시 `result.data` 에 포탈 원인분석 섹션이 들어갑니다. 대표 키: `kpi`, `market_size_series`, `brand_ranking`, `company_ranking`, `analysis_levels`, `analysis_level_market_status`, `level_top5_trend`, `target_customer_competition`, `target_customer_competition_by_channel`, `ubist_specialty_channels`, `ubist_specialty_target_channels`.\n" +
            "데이터/채널축이 없으면 `[]`, `{}`, 또는 note가 있는 fallback 객체로 반환됩니다.\n" +
            "\n" +
            "### 에러\n" +
            "\n" +
            "| 상황 | 응답 |\n" +
            "| --- | --- |\n" +
            "| 일반 검증 실패 | 400 `detail.error=invalid_dynamic_market_request` |\n" +
            "| scope 과다 | 400 `detail.error=dynamic_scope_too_broad` (+ `resolved_brand_rows`, `limit`) |\n" +
            "| 타입 검증 실패 (null 불가 필드에 null 등) | 422 |\n" +
            "\n" +
            "### Brand-Activity와의 필터 관계\n" +
            "- `/api/brand-activity/*` 도 같은 시장 필터 개념을 쓰지만 request model은 별도이며, 같은 Pydantic 클래스를 공유하지 않습니다.\n" +
            "- Dynamic-Market은 알 수 없는 필드를 `extra=forbid` 로 거절하지만, Brand-Activity는 중첩 필터 모델이 extra 값을 허용합니다.\n" +
            "- Brand-Activity handler는 일반뷰 시장 id를 flat `filters.atc4` 에서 읽으므로, nested `filters.atc.atc4` 만 보내면 400 (`filters.atc4 and selected_brand are required`)이 날 수 있습니다.\n" +
            "- Brand-Activity는 `filters` 가 비어 있으면 legacy `filter` 를 대신 쓰고, 둘 다 비어 있으면 빈 필터로 처리합니다."
    )
    @PostMapping("/dynamic")
    public ResponseEntity<Response> dynamic(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Dynamic marketRequest
    ) { return ResponseEntity.ok(marketService.getDynamic(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 동적 시장 필터 옵션",
        description = "포탈 필터 UI가 사용하는 옵션 목록입니다. 전략뷰는 시장 소속 ATC/차원을 한 번에 반환하고, 일반뷰는 선택된 ATC4 set 기준으로 소스별 scoped 옵션을 실시간 산출합니다."
    )
    @PostMapping("/dynamic/filter/options")
    public ResponseEntity<Response> dynamicFilterOptions(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Dynamic.FilterOptions marketRequest
    ) { return ResponseEntity.ok(marketService.getDynamicFilterOptions(accessToken, marketRequest)); }

    @Operation(summary = "브랜드 기본 ATC4 범위")
    @PostMapping("/dynamic/brand/default-scope")
    public ResponseEntity<Response> brandDefaultScope(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Dynamic.BrandDefaultScope marketRequest
    ) { return ResponseEntity.ok(marketService.getBrandDefaultScope(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 브랜드 기준 필터 옵션",
        description = "포탈 옵션 불러오기/기본 체크 상태 확인용 API입니다. filter-options와 같은 option list contract를 반환하며, brand_matched에는 선택 브랜드가 실제로 가진 ATC4·분석레벨 값이 들어갑니다. 신규 화면은 가능하면 /api/dynamic-market/filter-options를 사용하되, 브랜드 선택 직후 기본값 진단에는 이 endpoint를 호출합니다."
    )
    @PostMapping("/dynamic/brand/options")
    public ResponseEntity<Response> dynamicBrandOptions(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Dynamic.BrandOptions marketRequest
    ) { return ResponseEntity.ok(marketService.getDynamicBrandOptions(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 시장필터 1단계 ATC 옵션",
        description = "조회 전용 GET 엔드포인트입니다. 브랜드, 뷰, 공개 소스(ubist/iqvia)를 입력받아 ATC1/2/3/4 옵션을 key/level/parent/flag 형태로 반환합니다. flag=true는 선택 브랜드가 해당 ATC 노드에 속한다는 뜻이며, 프론트에서는 초기 선택/locked 표시 기준으로 사용합니다."
    )
    @PostMapping("/atc/options")
    public ResponseEntity<Response> atcOptions(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Atc.Options marketRequest
    ) { return ResponseEntity.ok(marketService.getAtcOptions(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 브랜드별 CSD 단건 상태 조회",
        description = "Market 브랜드별 CSD 단건 상태 조회"
    )
    @PostMapping("/brand/presence")
    public ResponseEntity<Response> Presence(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Presence marketRequest
    ) { return ResponseEntity.ok(marketService.getPresence(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 브랜드별 CSD 다중 상태 조회",
        description = "Market 브랜드별 CSD 다중 상태 조회 최대 50개"
    )
    @PostMapping("/brand/multiple/presence")
    public ResponseEntity<Response> PresenceMultiple(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Presence marketRequest
    ) { return ResponseEntity.ok(marketService.getPresenceMultiple(accessToken, marketRequest)); }

    @Operation(
        summary = "Market 브랜드별 토픽 그리드",
        description =
            "Brand-Activity 3종은 **Dynamic-Market과 같은 시장 필터 개념**을 쓰지만 request model은 별도입니다.\n" +
            "\n" +
            "### source\n" +
            "- 입력 **없음**\n" +
            "- Rx / 브랜드 랭킹: 서버 코드의 `iqvia_nsa` source 사용\n" +
            "- 활동 · 키워드: `CSD` / `keyword` 테이블 결합\n" +
            "\n" +
            "### 시장 범위 — ATC4\n" +
            "- `filters.atc4` 또는 BFF 호환 입력 `filters.atc.atc4` 를 보내면 서버가 flat `filters.atc4` 로 정규화합니다.\n" +
            "\n" +
            "### analysis_level\n" +
            "- 지원 입력은 **IQVIA audit code** 뿐입니다.\n" +
            "- 채널축 값 슬라이스: `filters.analysis_level.iqvia.audit_code`\n" +
            "- 옛 호환 입력 `filters.channel.audit_code` 도 같은 값으로 정규화됩니다.\n" +
            "\n" +
            "### 키워드 행 필터 (토픽 / interest 행 슬라이스)\n" +
            "- `visit_location`, `specialty`, `interest`, `prescription_evolution`, `period_start`, `period_end`\n" +
            "- 호환 입력 `filters.channel.visit_location`, `filters.channel.specialty` 도 flat 필드로 정규화됩니다.\n" +
            "\n" +
            "### 입력 정규화 요약\n" +
            "\n" +
            "| 호환 입력 | 정규화 결과 |\n" +
            "| --- | --- |\n" +
            "| `filters.atc.atc4` | `filters.atc4` |\n" +
            "| `filters.channel.audit_code` | `filters.analysis_level.iqvia.audit_code` |\n" +
            "| `filters.channel.visit_location` | flat `visit_location` |\n" +
            "| `filters.channel.specialty` | flat `specialty` |\n" +
            "\n" +
            "### missing / null 처리\n" +
            "\n" +
            "| 입력 | 동작 |\n" +
            "| --- | --- |\n" +
            "| `filters` / `filter` 생략 | 빈 필터 객체 |\n" +
            "| `filters:null` 또는 `filter:null` | **validation error** |\n" +
            "| `filters` + legacy `filter` 동시 전송 | 비어 있지 않은 `filters` 우선 |\n" +
            "\n" +
            "### unknown field 처리\n" +
            "- Brand-Activity request **top-level**: 알 수 없는 필드 무시\n" +
            "- **중첩 필터 객체**: 호환성을 위해 추가 필드 보존 가능\n" +
            "\n" +
            "### 주의\n" +
            "- **PACK DESC**: `pack_desc` 는 Dynamic-Market IQVIA 분석레벨 필터입니다. Brand-Activity 처리 경로에서는 **사용하지 않습니다.**\n" +
            "- **channel_axis**: 공개 요청 스키마에서 제거됐고 **validation error** 로 거절됩니다."
    )
    @PostMapping("/brand/topics")
    public ResponseEntity<Response> topics(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Topics marketRequest
    ) { return ResponseEntity.ok(marketService.getTopics(accessToken, marketRequest)); }

    @Operation(
        summary = "Market Brand 활동·처방 추세 조회",
        description =
            "**SD 활동량**은 `csd_channel_dynamics_stage` 의 `jw_channel='TOTAL'` 만 사용하므로 화면 관점의 `region=TOTAL` 입니다.\n" +
            "**IQVIA 처방 지표**(`unit` / `counting_unit` / `dosage_unit`)는 같은 분기축으로 정렬됩니다.\n" +
            "\n" +
            "Brand-Activity 3종은 **Dynamic-Market과 같은 시장 필터 개념**을 쓰지만 request model은 별도입니다.\n" +
            "\n" +
            "### source\n" +
            "- 입력 **없음**\n" +
            "- Rx / 브랜드 랭킹: 서버 코드의 `iqvia_nsa` source 사용\n" +
            "- 활동 · 키워드: `CSD` / `keyword` 테이블 결합\n" +
            "\n" +
            "### 시장 범위 — ATC4\n" +
            "- `filters.atc4` 또는 BFF 호환 입력 `filters.atc.atc4` 를 보내면 서버가 flat `filters.atc4` 로 정규화합니다.\n" +
            "\n" +
            "### analysis_level\n" +
            "- 지원 입력은 **IQVIA audit code** 뿐입니다.\n" +
            "- 채널축 값 슬라이스: `filters.analysis_level.iqvia.audit_code`\n" +
            "- 옛 호환 입력 `filters.channel.audit_code` 도 같은 값으로 정규화됩니다.\n" +
            "\n" +
            "### 키워드 행 필터 (토픽 / interest 행 슬라이스)\n" +
            "- `visit_location`, `specialty`, `interest`, `prescription_evolution`, `period_start`, `period_end`\n" +
            "- 호환 입력 `filters.channel.visit_location`, `filters.channel.specialty` 도 flat 필드로 정규화됩니다.\n" +
            "\n" +
            "### 입력 정규화 요약\n" +
            "\n" +
            "| 호환 입력 | 정규화 결과 |\n" +
            "| --- | --- |\n" +
            "| `filters.atc.atc4` | `filters.atc4` |\n" +
            "| `filters.channel.audit_code` | `filters.analysis_level.iqvia.audit_code` |\n" +
            "| `filters.channel.visit_location` | flat `visit_location` |\n" +
            "| `filters.channel.specialty` | flat `specialty` |\n" +
            "\n" +
            "### missing / null 처리\n" +
            "\n" +
            "| 입력 | 동작 |\n" +
            "| --- | --- |\n" +
            "| `filters` / `filter` 생략 | 빈 필터 객체 |\n" +
            "| `filters:null` 또는 `filter:null` | **validation error** |\n" +
            "| `filters` + legacy `filter` 동시 전송 | 비어 있지 않은 `filters` 우선 |\n" +
            "\n" +
            "### unknown field 처리\n" +
            "- Brand-Activity request **top-level**: 알 수 없는 필드 무시\n" +
            "- **중첩 필터 객체**: 호환성을 위해 추가 필드 보존 가능\n" +
            "\n" +
            "### 주의\n" +
            "- **PACK DESC**: `pack_desc` 는 Dynamic-Market IQVIA 분석레벨 필터입니다. Brand-Activity 처리 경로에서는 **사용하지 않습니다.**\n" +
            "- **channel_axis**: 공개 요청 스키마에서 제거됐고 **validation error** 로 거절됩니다."
    )
    @PostMapping("/brand/time/series")
    public ResponseEntity<Response> timeSeries(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.TimeSeries marketRequest
    ) { return ResponseEntity.ok(marketService.getCsdTimeSeries(accessToken, marketRequest)); }

    @Operation(
        summary = "Market Brand CSD 활동량·비율·순위 추세",
        description = "문서 Section 1 CSD Channeldynamics 시계열 API입니다. 기존 /csd-timeseries와 별도로 CSD jw_channel 선택, 회사축, 활동량 rank series를 제공합니다."
    )
    @PostMapping("/brand/activity/series")
    public ResponseEntity<Response> activitySeries(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Series marketRequest
    ) { return ResponseEntity.ok(marketService.getActivitySeries(accessToken, marketRequest)); }

    @Operation(
        summary = "Market Brand interest×처방빈도 버블",
        description =
            "버블 차트 축 구성입니다.\n" +
            "\n" +
            "| 요소 | 매핑 |\n" +
            "| --- | --- |\n" +
            "| X축 | `rx_frequency_score` |\n" +
            "| Y축 | `interest_score` |\n" +
            "| 버블 면적 | `event_count` |\n" +
            "| `market_average` | 화면의 점선 십자 기준선 |\n" +

            "\n" +
            "Brand-Activity 3종은 **Dynamic-Market과 같은 시장 필터 개념**을 쓰지만 request model은 별도입니다.\n" +
            "\n" +
            "### source\n" +
            "- 입력 **없음**\n" +
            "- Rx / 브랜드 랭킹: 서버 코드의 `iqvia_nsa` source 사용\n" +
            "- 활동 · 키워드: `CSD` / `keyword` 테이블 결합\n" +
            "\n" +
            "### 시장 범위 — ATC4\n" +
            "- `filters.atc4` 또는 BFF 호환 입력 `filters.atc.atc4` 를 보내면 서버가 flat `filters.atc4` 로 정규화합니다.\n" +
            "\n" +
            "### analysis_level\n" +
            "- 지원 입력은 **IQVIA audit code** 뿐입니다.\n" +
            "- 채널축 값 슬라이스: `filters.analysis_level.iqvia.audit_code`\n" +
            "- 옛 호환 입력 `filters.channel.audit_code` 도 같은 값으로 정규화됩니다.\n" +
            "\n" +
            "### 키워드 행 필터 (토픽 / interest 행 슬라이스)\n" +
            "- `visit_location`, `specialty`, `interest`, `prescription_evolution`, `period_start`, `period_end`\n" +
            "- 호환 입력 `filters.channel.visit_location`, `filters.channel.specialty` 도 flat 필드로 정규화됩니다.\n" +
            "\n" +
            "### 입력 정규화 요약\n" +
            "\n" +
            "| 호환 입력 | 정규화 결과 |\n" +
            "| --- | --- |\n" +
            "| `filters.atc.atc4` | `filters.atc4` |\n" +
            "| `filters.channel.audit_code` | `filters.analysis_level.iqvia.audit_code` |\n" +
            "| `filters.channel.visit_location` | flat `visit_location` |\n" +
            "| `filters.channel.specialty` | flat `specialty` |\n" +
            "\n" +
            "### missing / null 처리\n" +
            "\n" +
            "| 입력 | 동작 |\n" +
            "| --- | --- |\n" +
            "| `filters` / `filter` 생략 | 빈 필터 객체 |\n" +
            "| `filters:null` 또는 `filter:null` | **validation error** |\n" +
            "| `filters` + legacy `filter` 동시 전송 | 비어 있지 않은 `filters` 우선 |\n" +
            "\n" +
            "### unknown field 처리\n" +
            "- Brand-Activity request **top-level**: 알 수 없는 필드 무시\n" +
            "- **중첩 필터 객체**: 호환성을 위해 추가 필드 보존 가능\n" +
            "\n" +
            "### 주의\n" +
            "- **PACK DESC**: `pack_desc` 는 Dynamic-Market IQVIA 분석레벨 필터입니다. Brand-Activity 처리 경로에서는 **사용하지 않습니다.**\n" +
            "- **channel_axis**: 공개 요청 스키마에서 제거됐고 **validation error** 로 거절됩니다."
    )
    @PostMapping("/brand/matrix")
    public ResponseEntity<Response> matrix(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Matrix marketRequest
    ) { return ResponseEntity.ok(marketService.getMatrix(accessToken, marketRequest)); }

    @Operation(
        summary = "Market Brand INTEREST 3구분 3년 월간 시계열",
        description =
            "IQVIA CSD keyword INTEREST 3구분(VERY / SOMEWHAT / NOT USEFUL)의 브랜드별 월간 시계열입니다.\n" +
            "\n" +
            "| 요소 | 설명 |\n" +
            "| --- | --- |\n" +
            "| 기간 | 요청 파라미터 **없음**. 데이터 최신월 기준 **3년 전체**를 항상 반환 (프론트가 절단) |\n" +
            "| 값 | 브랜드별·시점별 3구분 `count` 와 브랜드 내 분모(`total_count`) 기준 `pct` |\n" +
            "| 빈 시점 | 데이터 없는 시점은 `null` |\n" +
            "\n" +
            "Brand-Activity 3종은 **Dynamic-Market과 같은 시장 필터 개념**을 쓰지만 request model은 별도입니다.\n" +
            "\n" +
            "### source\n" +
            "- 입력 **없음**\n" +
            "- Rx / 브랜드 랭킹: 서버 코드의 `iqvia_nsa` source 사용\n" +
            "- 활동 · 키워드: `CSD` / `keyword` 테이블 결합\n" +
            "\n" +
            "### 시장 범위 — ATC4\n" +
            "- `filters.atc4` 또는 BFF 호환 입력 `filters.atc.atc4` 를 보내면 서버가 flat `filters.atc4` 로 정규화합니다.\n" +
            "\n" +
            "### 경쟁 브랜드 선정\n" +
            "- Brand-Activity는 **IQVIA 전용**입니다.\n" +
            "- Dynamic-Market 일반뷰 IQVIA와 같은 6개 row 필터를 적용합니다: `mfr_name_kor`, `molecule_type`, `molecule_desc`, `pack_desc`, `strength`, `nhi_type`\n" +
            "- 각 차원 **안에서는 OR**, 차원끼리는 **AND** 입니다.\n" +
            "- 선택된 시장 필터 scope 안에서 매출 합계 기준 상위 5개 + 선택 브랜드를 항상 포함해 **최대 6개**를 반환합니다.\n" +
            "- quarter window가 있는 요청(CSD 계열)은 해당 window 합계, window가 없는 요청은 mart metric history 전체 합계를 사용합니다.\n" +
            "- tie는 `brand_key` 오름차순입니다.\n" +
            "\n" +
            "### analysis_level\n" +
            "- 지원 입력은 **IQVIA audit code** 뿐입니다.\n" +
            "- 채널축 값 슬라이스: `filters.analysis_level.iqvia.audit_code`\n" +
            "- 옛 호환 입력 `filters.channel.audit_code` 도 같은 값으로 정규화됩니다.\n" +
            "- 이 값은 경쟁 브랜드 선정 시 선택된 window의 audit code 매출 합계에 반영됩니다.\n" +
            "\n" +
            "### 키워드 행 필터 (토픽 / interest 행 슬라이스)\n" +
            "- `visit_location`, `specialty`, `interest`, `prescription_evolution`, `start_date`, `end_date`\n" +
            "- 호환 입력 `period_start`, `period_end`, `filters.channel.visit_location`, `filters.channel.specialty` 도 flat 필드로 정규화됩니다.\n" +
            "\n" +
            "### 입력 정규화 요약\n" +
            "\n" +
            "| 호환 입력 | 정규화 결과 |\n" +
            "| --- | --- |\n" +
            "| `filters.atc.atc4` | `filters.atc4` |\n" +
            "| `filters.channel.audit_code` | `filters.analysis_level.iqvia.audit_code` |\n" +
            "| `filters.channel.visit_location` | flat `visit_location` |\n" +
            "| `filters.channel.specialty` | flat `specialty` |\n" +
            "| `period_start` / `period_end` | flat `start_date` / `end_date` |\n" +
            "\n" +
            "### missing / null 처리\n" +
            "\n" +
            "| 입력 | 동작 |\n" +
            "| --- | --- |\n" +
            "| `filters` / `filter` 생략 | 빈 필터 객체 |\n" +
            "| `filters:null` 또는 `filter:null` | **validation error** |\n" +
            "| `filters` + legacy `filter` 동시 전송 | 비어 있지 않은 `filters` 우선 |\n" +
            "\n" +
            "### unknown field 처리\n" +
            "- Brand-Activity request **top-level**: 알 수 없는 필드 무시\n" +
            "- **중첩 필터 객체**: 호환성을 위해 추가 필드 보존 가능\n" +
            "\n" +
            "### 주의\n" +
            "- **PACK DESC**: `pack_desc` 는 canonical sidecar의 `dimension_type='pack'` 행과 매칭해 상위 경쟁 브랜드 후보를 좁힙니다.\n" +
            "- **channel_axis**: 공개 요청 스키마에서 제거됐고 **validation error** 로 거절됩니다."
    )
    @PostMapping("/brand/interest/time/series")
    public ResponseEntity<Response> timeSeries(
        @RequestHeader("Authorization-Access-Token") String accessToken,
        @RequestBody Market.Request.Interest.TimeSeries marketRequest
    ) { return ResponseEntity.ok(marketService.getInterestTimeseries(accessToken, marketRequest)); }
}
