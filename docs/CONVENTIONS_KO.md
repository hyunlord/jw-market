# 한글 주석 및 Rebuild 문서 규약

이 레포의 ETL, mart, cache 로직을 바꾸는 모든 변경은 코드만으로 배경을
추적할 수 있어야 한다. audit zip은 보조 증거이고, 장기 유지보수의 기준은
repo 안의 주석과 문서다.

## 필수 주석 형식

로직 변경이 있는 지점에는 한글로 다음 네 가지를 남긴다.

1. 무엇을 하는가
2. 왜 필요한가
3. 도메인 근거는 무엇인가
4. 어떤 대안을 검토했고 왜 기각했는가

예:

```python
# A2: IQVIA mart dimension_data도 catalog recode 라벨로 다시 묶는다.
# cache에서만 recode하면 화면은 맞아도 mart audit에는 raw NFC/pack이 남아
# false source of truth가 되므로, layer3 집계 시점부터 raw label을 제거한다.
# UBIST와 다른 dimension은 기대 경로라 건드리지 않는다.
```

## 문서 갱신

- rebuild, migration, payload contract 변경은 `docs/rebuild/`에 설계 문서를 남긴다.
- 사용자 화면/도메인 계약을 바꾸는 변경은 `docs/CHANGELOG.md`에 항목을 추가한다.
- "테스트만 통과"는 충분하지 않다. 왜 그 테스트가 계약을 대표하는지 문서나
  주석으로 연결한다.

## 금지

- class recode를 molecule 표시값으로 쓰는 cross-field fallback 금지
- cache에서만 label을 고치고 mart raw를 남기는 이중 source 금지
- Excel 원본 변경을 시장별 hardcode로 덮는 방식 금지
- 보호 파일이나 로컬 auth shim을 rebuild commit에 섞는 것 금지

## Commit 전 체크

- py_compile 또는 해당 언어의 최소 문법 검증
- 주석/문서 변경만 하는 작업은 AST 또는 주석 제거 diff로 로직 불변 증명
- rebuild 작업은 게이트 이름(G1~G6 등)과 audit SHA를 커밋 메시지나 문서에 기록

