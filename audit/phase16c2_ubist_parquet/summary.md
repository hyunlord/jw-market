# Phase 16-C-2 UBIST Parquet Load Summary

Generated: 2026-05-19T01:24:44

## Overall

PASS. UBIST 53 xlsx files were loaded into hive-partitioned Parquet.

## Architecture

- UBIST storage: `parquet/ubist/year=YYYY/month=MM/data.parquet`
- Compression: snappy
- Schema: wide-on-metric
- Metrics: `rx_amt`, `rx_cnt`, `rx_qty`
- Dimensions: 19 canonical common dimensions + 6 patent dimensions (nullable)
- Manifest: `parquet/ubist/_manifest.json`
- MariaDB: `ubist_monthly_sales_raw` dropped by migration 003; IQVIA raw tables retained.

## Duplicate Dimension Decision

Sample audit found identical values for the duplicate fixed dimension headers across 10 representative sheets / 20,000 sampled rows per pair:

- `제조사` A vs J: collapse, keep first occurrence
- `국내/외자` B vs D: collapse, keep first occurrence
- `판매사` C vs H: collapse, keep first occurrence
- `ATC` F vs P: collapse, keep first occurrence
- `성분` L vs Q: collapse, keep first occurrence
- `판매사2`: retained as distinct explicit header

## Load Results

- Source files: 53
- Partitions: 64
- Period range: 2021-01 .. 2026-04
- Total rows: 145,384,564
- Disk usage: `2.4G	parquet/ubist`
- Load strategy: all 53 xlsx in one streaming replace load; partition writers avoided full in-memory materialization.

## Verification

```text
# UBIST Parquet Verification

## Partition row counts
period_yyyymm    rows
      2021-01 2091505
      2021-02 2091505
      2021-03 2091505
      2021-04 2125305
      2021-05 2125305
      2021-06 2125305
      2021-07 2138203
      2021-08 2138203
      2021-09 2138203
      2021-10 1211970
      2021-11 1211970
      2021-12 1211970
      2022-01 2238966
      2022-02 2238966
      2022-03 2238966
      2022-04 2251968
      2022-05 2251968
      2022-06 2251968
      2022-07 2266158
      2022-08 2266158
      2022-09 2266158
      2022-10 2278990
      2022-11 2278990
      2022-12 2278990
      2023-01 2354183
      2023-02 2354183
      2023-03 2354183
      2023-04 2372226
      2023-05 2372226
      2023-06 2372226
      2023-07 2372419
      2023-08 2372419
      2023-09 2372419
      2023-10 2377271
      2023-11 2377271
      2023-12 2377271
      2024-01 2331786
      2024-02 2331786
      2024-03 2337761
      2024-04 2337761
      2024-05 2338633
      2024-06 2338633
      2024-07 2358771
      2024-08 2358771
      2024-09 2363998
      2024-10 2363998
      2024-11 2372256
      2024-12 2372256
      2025-01 2418686
      2025-02 2418686
      2025-03 2428633
      2025-04 2428633
      2025-05 1443937
      2025-06 2422130
      2025-07 2422130
      2025-08 2441250
      2025-09 2441250
      2025-10 2278901
      2025-11 2284076
      2025-12 2297451
      2026-01 2366636
      2026-02 4285520
      2026-03 2278970
      2026-04 2283773

partition_count: 64
period_min: 2021-01
period_max: 2026-04
row_sum: 145384564

## Total rows
Total: 145,384,564

## Unique 약품코드
Unique 약품코드: 20,461

## 종별 distribution
              종별     rows
              의원 93073303
          상급종합병원 23857233
              병원 12691742
기타(치과의원, 치과병원 등)  7989564
             보건소  5255091
            종합병원  2517631

## 진료과 distribution top 30
                      진료과     rows
Others(병원,보건기관, 그 외 요양기관) 25936397
                   내과(IM) 20756760
               분리되지 않은 내과 17243676
                  일반의(GP) 13228945
                가정의학과(FM)  7319239
               이비인후과(ENT)  6134921
                 정형외과(OS)  5595627
                   외과(GS)  4553670
              소아청소년과(PED)  4411956
                  신경과(NR)  4151095
                 신경외과(NS)  3597965
                 피부과(DER)  3577804
               비뇨의학과(URO)  3565935
                  안과(OPH)  2973591
            마취통증의학과(ANES)  2710935
               산부인과(OBGY)  2180616
         unknown(종합병원 이상)  2171082
                재활의학과(RM)  2017716
       순환기(Cardiology IM)  1639240
 소화기(Gastroenterology IM)  1318210
            정신건강의학과(PSYC)  1306334
    내분비(Endocrinology IM)  1254308
                 성형외과(PS)  1204565
        신장(Nephrology IM)  1107698
 혈액종양(Hemoto Oncology IM)   901890
      호흡기(Pulmonology IM)   826269
         영상의학과(Radiology)   821040
    류마티스(Rheumatology IM)   809469
             심장혈관흉부외과(CS)   745953
 감염(Infection Disease IM)   353347

## Metric null distribution
    total  null_amt  null_cnt  null_qty
145384564       0.0       0.0       0.0

## Basic assertions
PASS: 64 partitions, period 2021-01..2026-04, total rows match partition sum

```

## Git Notes

- New hive partition data files are intentionally ignored by `.gitignore`.
- Existing tracked flat `parquet/ubist/YYYY-MM.parquet` files are removed by the architecture replacement.
- `_manifest.json`, loader code, migration, and audit artifacts are the tracked handoff surface.
