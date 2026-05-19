# IQVIA CHSO Dry Run

- files: 1
## File: `data/IQVIA/CHSO/CHSO_KOR_SellOut_Basic_Feb-19-2026(2026년 1월까지).xlsx`
- period count: 60
- period range: 2021-02 .. 2026-01
- metrics: ['SELL IN PRICE', 'SELL OUT PRICE AVERAGE', 'UNITS', 'VALUES LC SI PRICE']
- first columns: ['AUDIT DESC\n', 'MFR NAME KOR\n', 'PRODUCT NAME KOR\n', 'PACK DESCRIPTION\n', 'CHC 1\n', 'CHC 2\n', 'CHC 3\n', 'CHC 4\n', 'ATC 1\n', 'ATC 2\n', 'ATC 3\n', 'ATC 4\n', 'VALUES LC SI PRICE\n2/2021', 'VALUES LC SI PRICE\n3/2021', 'VALUES LC SI PRICE\n4/2021', 'VALUES LC SI PRICE\n5/2021', 'VALUES LC SI PRICE\n6/2021', 'VALUES LC SI PRICE\n7/2021', 'VALUES LC SI PRICE\n8/2021', 'VALUES LC SI PRICE\n9/2021']
- preview records generated before stop: 3

## Sample records
```json
{
  "source_file": "CHSO_KOR_SellOut_Basic_Feb-19-2026(2026년 1월까지).xlsx",
  "sheet_name": "Sell Out  Standard",
  "source_row_no": 2,
  "period_yyyymm": "2025-05",
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"CHSO\",\"__raw_period\":\"2025-05\",\"__period_metric_columns\":{\"VALUES LC SI PRICE\":\"VALUES LC SI PRICE 5/2025\",\"UNITS\":\"UNITS 5/2025\",\"SELL OUT PRICE AVERAGE\":\"SELL OUT PRICE AVERAGE 5/2025\",\"SELL IN PRICE\":\"SELL IN PRICE 5/2025\"},\"static\":{\"AUDIT DESC\":\"Sell_Out\",\"MFR NAME KOR\":\"ALINAMIN PHARMA\",\"PRODUCT NAME KOR\":\"액티넘이엑스골드\",\"PACK DESCRIPTION\":\"DRG 180\",\"CHC 1\":\"04_VITAM.MINER.&NUTRIT.SUPPL\",\"CHC 2\":\"04C_VITAMIN B GROUP\",\"CHC 3\":\"04C4_VITAMIN B COMBINATIONS\",\"CHC 4\":\"04C4C_VITAMIN B COMBIN.CAPS/TAB\",\"ATC 1\":\"A_ALIMENTARY T.& METABOLISM\",\"ATC 2\":\"A11_VITAMINS\",\"ATC 3\":\"A11D_VITAMIN B1 & COMBINATION\",\"ATC 4\":\"A11D9_OTHER VITAMIN B1 COMBS\"},\"period_values\":{\"VALUES LC SI PRICE\":961233,\"UNITS\":21,\"SELL OUT PRICE AVERAGE\":55954.2857142857,\"SELL IN PRICE\":45773}}"
}
```
```json
{
  "source_file": "CHSO_KOR_SellOut_Basic_Feb-19-2026(2026년 1월까지).xlsx",
  "sheet_name": "Sell Out  Standard",
  "source_row_no": 2,
  "period_yyyymm": "2025-06",
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"CHSO\",\"__raw_period\":\"2025-06\",\"__period_metric_columns\":{\"VALUES LC SI PRICE\":\"VALUES LC SI PRICE 6/2025\",\"UNITS\":\"UNITS 6/2025\",\"SELL OUT PRICE AVERAGE\":\"SELL OUT PRICE AVERAGE 6/2025\",\"SELL IN PRICE\":\"SELL IN PRICE 6/2025\"},\"static\":{\"AUDIT DESC\":\"Sell_Out\",\"MFR NAME KOR\":\"ALINAMIN PHARMA\",\"PRODUCT NAME KOR\":\"액티넘이엑스골드\",\"PACK DESCRIPTION\":\"DRG 180\",\"CHC 1\":\"04_VITAM.MINER.&NUTRIT.SUPPL\",\"CHC 2\":\"04C_VITAMIN B GROUP\",\"CHC 3\":\"04C4_VITAMIN B COMBINATIONS\",\"CHC 4\":\"04C4C_VITAMIN B COMBIN.CAPS/TAB\",\"ATC 1\":\"A_ALIMENTARY T.& METABOLISM\",\"ATC 2\":\"A11_VITAMINS\",\"ATC 3\":\"A11D_VITAMIN B1 & COMBINATION\",\"ATC 4\":\"A11D9_OTHER VITAMIN B1 COMBS\"},\"period_values\":{\"VALUES LC SI PRICE\":2792153,\"UNITS\":61,\"SELL OUT PRICE AVERAGE\":58424.262295082,\"SELL IN PRICE\":45773}}"
}
```
```json
{
  "source_file": "CHSO_KOR_SellOut_Basic_Feb-19-2026(2026년 1월까지).xlsx",
  "sheet_name": "Sell Out  Standard",
  "source_row_no": 2,
  "period_yyyymm": "2025-07",
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"CHSO\",\"__raw_period\":\"2025-07\",\"__period_metric_columns\":{\"VALUES LC SI PRICE\":\"VALUES LC SI PRICE 7/2025\",\"UNITS\":\"UNITS 7/2025\",\"SELL OUT PRICE AVERAGE\":\"SELL OUT PRICE AVERAGE 7/2025\",\"SELL IN PRICE\":\"SELL IN PRICE 7/2025\"},\"static\":{\"AUDIT DESC\":\"Sell_Out\",\"MFR NAME KOR\":\"ALINAMIN PHARMA\",\"PRODUCT NAME KOR\":\"액티넘이엑스골드\",\"PACK DESCRIPTION\":\"DRG 180\",\"CHC 1\":\"04_VITAM.MINER.&NUTRIT.SUPPL\",\"CHC 2\":\"04C_VITAMIN B GROUP\",\"CHC 3\":\"04C4_VITAMIN B COMBINATIONS\",\"CHC 4\":\"04C4C_VITAMIN B COMBIN.CAPS/TAB\",\"ATC 1\":\"A_ALIMENTARY T.& METABOLISM\",\"ATC 2\":\"A11_VITAMINS\",\"ATC 3\":\"A11D_VITAMIN B1 & COMBINATION\",\"ATC 4\":\"A11D9_OTHER VITAMIN B1 COMBS\"},\"period_values\":{\"VALUES LC SI PRICE\":1510509,\"UNITS\":33,\"SELL OUT PRICE AVERAGE\":72532.1212121212,\"SELL IN PRICE\":45773}}"
}
```
