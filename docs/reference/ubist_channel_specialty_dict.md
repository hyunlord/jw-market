# UBIST 종별 / 진료과 사전

## 종별 매핑 (UBIST raw → MI팀 표기)
| UBIST raw label | MI팀 표기 |
|---|---|
| 상급종합병원 | TH |
| 종합병원 | GH |
| 병원 | Semi |
| 의원 | CL |
| 보건소 | 기타 |
| 기타 | 기타 |
| 기타(치과의원, 치과병원 등) | 기타 |

## 진료과 매핑 (UBIST raw → MI팀 표기)
| UBIST raw label | MI팀 표기 |
|---|---|
| 가정의학과(FM) | IGF |
| 내과(IM) | IGF |
| 일반의(GP) | IGF |
| 순환기(Cardiology IM) | Cardio |
| 소화기(Gastroenterology IM) | GI |
| 내분비(Endocrinology IM) | Endo |
| 신장(Nephrology IM) | Nephro |
| 신경과(NR) | Neuro |
| 비뇨의학과(URO) | Uro |
| Others(병원,보건기관, 그 외 요양기관) | 기타 |

## target_ubist label format
`<종별> <진료과>` 예: `GH GI`, `CL IGF`, `TH Cardio`.

## 출처
MI팀_시장분석 AI_시장 분석 Master Version (260422) workbook 의 UBIST 채널 sheet 기준으로 정리하고, 실제 raw value inventory 로 `기타(치과의원, 치과병원 등)` 및 `Others(병원,보건기관, 그 외 요양기관)` fallback 을 추가 확인.
