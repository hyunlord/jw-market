# IQVIA NSA Dry Run

- files: 1
## File: `data/IQVIA/NSA/NSA_IQVIA_2025 4Q.csv`
- period count: 20
- period range: 2021-03 .. 2025-12
- metrics: ['Counting Units', 'Dosage Units', 'Units', 'Values LC']
- first columns: ['AUDIT CODE', 'OTC/ETHICAL', 'MFR CODE', 'MFR NAME', 'MFR NAME KOR', 'MFT TYPE', 'MFR TYPE GROUP', 'ATC 1 CODE', 'ATC 1 DESC', 'ATC 2 CODE', 'ATC 2 DESC', 'ATC 3 CODE', 'ATC 3 DESC', 'ATC 4 CODE', 'ATC 4 DESC', 'PRODUCT NAME', 'PRODUCT NAME KOR', 'PRODUCT LAUNCH DATE', 'PRODUCT AGE', 'NFC 1 CODE']
- preview records generated before stop: 3

## Sample records
```json
{
  "source_file": "NSA_IQVIA_2025 4Q.csv",
  "sheet_name": "CSV",
  "source_row_no": 2,
  "audit_code": "KCPA_DIRECT",
  "audit_desc": "KCPA_DIRECT",
  "mfr_code": "A+K",
  "mfr_name": "AUSKOREA",
  "period_yyyy": 2023,
  "period_quarter": 4,
  "period_label": "2023Q4",
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"NSA\",\"__raw_period\":\"2023-12\",\"__period_metric_columns\":{\"Values LC\":\"12/2023_Values LC\",\"Units\":\"12/2023_Units\",\"Counting Units\":\"12/2023_Counting Units\",\"Dosage Units\":\"12/2023_Dosage Units\"},\"static\":{\"AUDIT CODE\":\"KCPA_DIRECT\",\"OTC/ETHICAL\":\"ETHICAL\",\"MFR CODE\":\"A+K\",\"MFR NAME\":\"AUSKOREA\",\"MFR NAME KOR\":\"오스코리아\",\"MFT TYPE\":\"LOCAL\",\"MFR TYPE GROUP\":\"LOCAL\",\"ATC 1 CODE\":\"A\",\"ATC 1 DESC\":\"ALIMENTARY T.& METABOLISM\",\"ATC 2 CODE\":\"A11\",\"ATC 2 DESC\":\"VITAMINS\",\"ATC 3 CODE\":\"A11G\",\"ATC 3 DESC\":\"VIT C INC.MINERAL COMBS\",\"ATC 4 CODE\":\"A11G1\",\"ATC 4 DESC\":\"VITAMIN C PLAIN\",\"PRODUCT NAME\":\"AUCORBIC\",\"PRODUCT NAME KOR\":\"오코빅\",\"PRODUCT LAUNCH DATE\":\"8/2015\",\"PRODUCT AGE\":\"125\",\"NFC 1 CODE\":\"F\",\"NFC 1 DESC\":\"PARENTERAL ORDINARY\",\"NFC 2 CODE\":\"FP\",\"NFC 2 DESC\":\"PARENT ORD VIALS\",\"NFC 3 COD"
}
```
```json
{
  "source_file": "NSA_IQVIA_2025 4Q.csv",
  "sheet_name": "CSV",
  "source_row_no": 3,
  "audit_code": "KCPA_DIRECT",
  "audit_desc": "KCPA_DIRECT",
  "mfr_code": "A+K",
  "mfr_name": "AUSKOREA",
  "period_yyyy": 2021,
  "period_quarter": 3,
  "period_label": "2021Q3",
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"NSA\",\"__raw_period\":\"2021-09\",\"__period_metric_columns\":{\"Values LC\":\"9/2021_Values LC\",\"Units\":\"9/2021_Units\",\"Counting Units\":\"9/2021_Counting Units\",\"Dosage Units\":\"9/2021_Dosage Units\"},\"static\":{\"AUDIT CODE\":\"KCPA_DIRECT\",\"OTC/ETHICAL\":\"ETHICAL\",\"MFR CODE\":\"A+K\",\"MFR NAME\":\"AUSKOREA\",\"MFR NAME KOR\":\"오스코리아\",\"MFT TYPE\":\"LOCAL\",\"MFR TYPE GROUP\":\"LOCAL\",\"ATC 1 CODE\":\"J\",\"ATC 1 DESC\":\"SYSTEMIC ANTI-INFECTIVES\",\"ATC 2 CODE\":\"J01\",\"ATC 2 DESC\":\"SYSTEMIC ANTIBACTERIALS\",\"ATC 3 CODE\":\"J01D\",\"ATC 3 DESC\":\"CEPHALOSPORINS & COMBS\",\"ATC 4 CODE\":\"J01D2\",\"ATC 4 DESC\":\"INJECTABLE CEPHALOSPORINS\",\"PRODUCT NAME\":\"ASTRIAXONE\",\"PRODUCT NAME KOR\":\"아스트리악손\",\"PRODUCT LAUNCH DATE\":\"11/2016\",\"PRODUCT AGE\":\"110\",\"NFC 1 CODE\":\"F\",\"NFC 1 DESC\":\"PARENTERAL ORDINARY\",\"NFC 2 CODE\":\"FP\",\"NFC 2 DESC\":\"PAR"
}
```
```json
{
  "source_file": "NSA_IQVIA_2025 4Q.csv",
  "sheet_name": "CSV",
  "source_row_no": 4,
  "audit_code": "KCPA_DIRECT",
  "audit_desc": "KCPA_DIRECT",
  "mfr_code": "A+K",
  "mfr_name": "AUSKOREA",
  "period_yyyy": 2021,
  "period_quarter": 4,
  "period_label": "2021Q4",
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"NSA\",\"__raw_period\":\"2021-12\",\"__period_metric_columns\":{\"Values LC\":\"12/2021_Values LC\",\"Units\":\"12/2021_Units\",\"Counting Units\":\"12/2021_Counting Units\",\"Dosage Units\":\"12/2021_Dosage Units\"},\"static\":{\"AUDIT CODE\":\"KCPA_DIRECT\",\"OTC/ETHICAL\":\"ETHICAL\",\"MFR CODE\":\"A+K\",\"MFR NAME\":\"AUSKOREA\",\"MFR NAME KOR\":\"오스코리아\",\"MFT TYPE\":\"LOCAL\",\"MFR TYPE GROUP\":\"LOCAL\",\"ATC 1 CODE\":\"J\",\"ATC 1 DESC\":\"SYSTEMIC ANTI-INFECTIVES\",\"ATC 2 CODE\":\"J01\",\"ATC 2 DESC\":\"SYSTEMIC ANTIBACTERIALS\",\"ATC 3 CODE\":\"J01D\",\"ATC 3 DESC\":\"CEPHALOSPORINS & COMBS\",\"ATC 4 CODE\":\"J01D2\",\"ATC 4 DESC\":\"INJECTABLE CEPHALOSPORINS\",\"PRODUCT NAME\":\"ASTRIAXONE\",\"PRODUCT NAME KOR\":\"아스트리악손\",\"PRODUCT LAUNCH DATE\":\"11/2016\",\"PRODUCT AGE\":\"110\",\"NFC 1 CODE\":\"F\",\"NFC 1 DESC\":\"PARENTERAL ORDINARY\",\"NFC 2 CODE\":\"FP\",\"NFC 2 DESC\":"
}
```
