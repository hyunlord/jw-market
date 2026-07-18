# 라이브 CM 캡처 (2026-07-16, kubectl get -o yaml)

정본 코드는 이 디렉터리의 상위에 있는 `.py`들이다 — `row_topic_monthly_wrapper.py`는
라이브 ConfigMap 내장 코드와 md5 일치, topic_monthly_job 코드는 develop에 정본화돼 있다.
여기 캡처본은 "라이브가 실제로 무엇을 실행 중인가"의 스냅샷 증거이며, CronJob이
ConfigMap 내장 코드를 실행하는 패턴 자체(레포 밖 실행체)는 파이프라인 오너 트랙에서
이미지-내장 실행으로 전환하는 것이 권고 상태다(정본화 STAGE 4 · CronJob manifest 소관 경계).
드리프트 확인: 캡처 vs 상위 `.py`/`cronjob_topic_monthly.yaml` diff.
