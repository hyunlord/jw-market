# UBIST Parquet Loader Dry Run

## Source: /Users/rexxa/github/jw-market-test/data/UBIST/Sales (2021-2026.02)/보건소/보건소 2021-2022.xlsx

### Sheet: Sheet1
- rows: 67,845
- cols: 96
- kept dimensions: 19
  - col 1: 제조사 -> 제조사
  - col 2: 국내/외자 -> 국내/외자
  - col 3: 판매사 -> 판매사
  - col 5: 제품 -> 제품
  - col 6: ATC -> ATC
  - col 7: 브랜드 -> 브랜드
  - col 9: 판매사2 -> 판매사2
  - col 11: 약가 -> 약가
  - col 12: 성분 -> 성분
  - col 13: 성분용량 -> 성분용량
  - col 14: 일반/전문 -> 일반/전문
  - col 15: 약품코드 -> 약품코드
  - col 18: 제형 -> 제형
  - col 19: 투여경로 -> 투여경로
  - col 20: 급여구분 -> 급여구분
  - col 21: 종별 -> 종별
  - col 22: 진료과 -> 진료과
  - col 23: 연령 -> 연령
  - col 24: 성별 -> 성별
- duplicate dimensions dropped: 5
  - col 4: 국내/외자 -> 국내/외자
  - col 8: 판매사 -> 판매사
  - col 10: 제조사 -> 제조사
  - col 16: ATC -> ATC
  - col 17: 성분 -> 성분
- metrics: ['rx_amt', 'rx_cnt', 'rx_qty']
- periods: 2021-01 .. 2022-12 (24)

#### Sample Output Rows
제조사 국내/외자 판매사 판매사2            제품                        ATC  브랜드   약가       성분                        성분용량 일반/전문      약품코드      제형 투여경로 급여구분  종별                       진료과     연령 성별 물질특허만료일 마지막특허만료일 마지막특허특성 PMS만료일 약품허가일 Generic   rx_amt  rx_cnt  rx_qty period_yyyymm        source_file            source_folder source_sheet  source_row_no               ingested_at
갈더마    외자 갈더마 None 데스오웬 로션 0.05% [D7A] 외용 코르티코스테로이드제 (단일제제) 데스오웬 5546 desonide desonide 0.5㎎/g [141543CLT]    전문 655700021 로션제(LT)   외용   급여 보건소 Others(병원,보건기관, 그 외 요양기관) 10세 미만  남    None     None    None   None  None    None 73353.92    12.0   13.12       2021-01 보건소 2021-2022.xlsx Sales (2021-2026.02)/보건소       Sheet1              3 2026-05-19T00:08:15+09:00
갈더마    외자 갈더마 None 데스오웬 로션 0.05% [D7A] 외용 코르티코스테로이드제 (단일제제) 데스오웬 5546 desonide desonide 0.5㎎/g [141543CLT]    전문 655700021 로션제(LT)   외용   급여 보건소 Others(병원,보건기관, 그 외 요양기관) 10세 미만  남    None     None    None   None  None    None     0.00     0.0    0.00       2021-02 보건소 2021-2022.xlsx Sales (2021-2026.02)/보건소       Sheet1              3 2026-05-19T00:08:15+09:00
갈더마    외자 갈더마 None 데스오웬 로션 0.05% [D7A] 외용 코르티코스테로이드제 (단일제제) 데스오웬 5546 desonide desonide 0.5㎎/g [141543CLT]    전문 655700021 로션제(LT)   외용   급여 보건소 Others(병원,보건기관, 그 외 요양기관) 10세 미만  남    None     None    None   None  None    None     0.00     0.0    0.00       2021-03 보건소 2021-2022.xlsx Sales (2021-2026.02)/보건소       Sheet1              3 2026-05-19T00:08:15+09:00
