# IQVIA CSD Dry Run

- files: 1
## File: `data/IQVIA/CSD/ChannelDynamics (콜 수=영업 횟수)/CSD_ChannelDynamics_JW Pharma Regional Report_Oct.25.xlsx`
- sheets: ['Main', 'TOTAL-Call Rank Monthly', 'TOTAL - TOP20 Monthly', 'TOTAL-Detail Monthly', 'TOTAL-Product Detail Monthly', 'TOTAL Product - Detail Monthly', 'GH-Team Trend Monthly', 'GH-Call Rank Monthly', 'GH - TOP10 Monthly', 'GH-Detail Monthly', 'GH Product - Detail Monthly', 'GH-Product Detail Monthly', 'SHPPI-Team Trend Monthly', 'SHPPI-Call Rank Monthly', 'SHPPI - TOP10 Monthly', 'SHPPI-Detail Monthly', 'SHPPI Product - Detail Monthly', 'SHPPI-Product Detail Monthly', 'GH + SHPPI-Team Trend Monthly', 'GH + SHPPI-Call Rank Monthly', 'GH + SHPPI- TOP10 Monthly', 'GH + SHPPI-Detail Monthly', 'GH+SHPPI Product-Detail Monthly', 'GH+SHPPI-Product Detail Monthly', 'CPPI-Team Trend Monthly', 'CPPI-Call Rank Monthly', 'CPPI - TOP10 Monthly', 'CPPI-Detail Monthly', 'CPPI Product - Detail Monthly', 'CPPI-Product Detail Monthly', 'LIVALO Market', 'GUARDLET Market', 'PPI Market', 'GANAKHAN Market', 'TURUPAS Market', 'FERINJECT Market', 'FOSRENOL Market', 'ENCOVER Market', 'WINUF Market', 'PLAJU OP Market', 'LIVALO V Market', 'LIVALOZET Market', 'LIVALOZET Market2']
  - Main: header_row=None, meta={}
  - TOTAL-Call Rank Monthly: header_row=7, meta={'Data Type': 'Monthly', 'Value Type': 'Weighted Calls', 'Specialty': 'GH + SHPPI + CPPI', 'Hospital Type': 'TOTAL(GH+SHPPI+CPPI)'}
  - TOTAL - TOP20 Monthly: header_row=7, meta={'Data Type': 'Monthly', 'Value Type': 'Product Detail', 'Specialty': 'GH + SHPPI + CPPI', 'Hospital Type': 'TOTAL(GH+SHPPI+CPPI)'}
  - TOTAL-Detail Monthly: header_row=8, meta={'Data Type': 'Monthly', 'Value Type': 'Weighted Calls', 'Specialty': 'GH + SHPPI + CPPI', 'Hospital Type': 'TOTAL(GH+SHPPI+CPPI)'}
  - TOTAL-Product Detail Monthly: header_row=7, meta={'Data Type': 'Monthly', 'Value Type': 'Product Detail', 'Specialty': 'GH + SHPPI + CPPI', 'Hospital Type': 'TOTAL(GH+SHPPI+CPPI)'}
- preview records generated before stop: 3

## Sample records
```json
{
  "source_file": "CSD_ChannelDynamics_JW Pharma Regional Report_Oct.25.xlsx",
  "sheet_name": "TOTAL-Call Rank Monthly",
  "source_row_no": 8,
  "period_yyyymm": "2025-10",
  "channel": "GH+SHPPI+CPPI",
  "region": null,
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"CSD\",\"__header_row\":7,\"__metadata\":{\"Data Type\":\"Monthly\",\"Value Type\":\"Weighted Calls\",\"Specialty\":\"GH + SHPPI + CPPI\",\"Hospital Type\":\"TOTAL(GH+SHPPI+CPPI)\"},\"row\":{\"JW Channel\":\"GH+SHPPI+CPPI\",\"Rank\":\"1\",\"Representing Company\":\"YUHAN CO.\",\"Nov. 24\":75036,\"Dec. 24\":71014,\"Jan. 25\":75816,\"Feb. 25\":78853,\"Mar. 25\":84391,\"Apr. 25\":80916,\"May 25\":78090,\"June 25\":79482,\"July 25\":82387,\"Aug. 25\":79793,\"Sep. 25\":82000,\"Oct. 25\":78261}}"
}
```
```json
{
  "source_file": "CSD_ChannelDynamics_JW Pharma Regional Report_Oct.25.xlsx",
  "sheet_name": "TOTAL-Call Rank Monthly",
  "source_row_no": 9,
  "period_yyyymm": "2025-10",
  "channel": "TOTAL(GH+SHPPI+CPPI)",
  "region": null,
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"CSD\",\"__header_row\":7,\"__metadata\":{\"Data Type\":\"Monthly\",\"Value Type\":\"Weighted Calls\",\"Specialty\":\"GH + SHPPI + CPPI\",\"Hospital Type\":\"TOTAL(GH+SHPPI+CPPI)\"},\"row\":{\"JW Channel\":null,\"Rank\":\"2\",\"Representing Company\":\"CHONG KUN DANG\",\"Nov. 24\":51318,\"Dec. 24\":55126,\"Jan. 25\":52820,\"Feb. 25\":58794,\"Mar. 25\":62950,\"Apr. 25\":61212,\"May 25\":61313,\"June 25\":63478,\"July 25\":64543,\"Aug. 25\":57209,\"Sep. 25\":62606,\"Oct. 25\":57064}}"
}
```
```json
{
  "source_file": "CSD_ChannelDynamics_JW Pharma Regional Report_Oct.25.xlsx",
  "sheet_name": "TOTAL-Call Rank Monthly",
  "source_row_no": 10,
  "period_yyyymm": "2025-10",
  "channel": "TOTAL(GH+SHPPI+CPPI)",
  "region": null,
  "source_master_version": null,
  "payload_preview": "{\"__source\":\"CSD\",\"__header_row\":7,\"__metadata\":{\"Data Type\":\"Monthly\",\"Value Type\":\"Weighted Calls\",\"Specialty\":\"GH + SHPPI + CPPI\",\"Hospital Type\":\"TOTAL(GH+SHPPI+CPPI)\"},\"row\":{\"JW Channel\":null,\"Rank\":\"3\",\"Representing Company\":\"HAN MI\",\"Nov. 24\":54628,\"Dec. 24\":53255,\"Jan. 25\":55215,\"Feb. 25\":52782,\"Mar. 25\":56534,\"Apr. 25\":51449,\"May 25\":55287,\"June 25\":52941,\"July 25\":54101,\"Aug. 25\":58987,\"Sep. 25\":56256,\"Oct. 25\":51055}}"
}
```
