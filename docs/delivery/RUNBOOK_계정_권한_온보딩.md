# 계정·권한 온보딩 런북

이 문서는 JW Market 운영에 필요한 계정과 권한의 신청, 승인, 발급, 검증, 정기 검토, 회수를 정의한다. 비밀번호·토큰·키·개인 계정 값은 문서와 Git에 기록하지 않는다.

## 1. 원칙

- 개인별 계정을 사용하고 공유 계정은 자동화용 service account에만 허용한다.
- 최소 권한, 업무 분리, 만료일, 승인자, 회수 책임자를 모든 신청에 기록한다.
- 개발·test2·운영 권한을 분리한다. 운영 write와 Secret read는 별도 승인이다.
- 발급자는 신청자와 승인자가 될 수 없다.
- Secret 값은 승인된 Secret Manager/Kubernetes Secret에만 저장한다.

## 2. 권한 영역

| 영역 | 대표 권한 | 기본 승인자 | 확인 방법(값 노출 없음) |
|---|---|---|---|
| GitHub `hyunlord/jw-market` | read / branch push / develop integration | 코드 소유자 | `git ls-remote jw-private develop` |
| Gitea `jw-market/jw-data-input` | read / feature push / protected branch integration | 사이트 코드 소유자 | Gitea UI의 사용자·팀 권한 |
| GKE `llmops` | view / logs / exec / deploy patch | 플랫폼 + 서비스 소유자 | `kubectl auth can-i <verb> <resource> -n llmops` |
| Artifact Registry | pull / push | 플랫폼 | `gcloud artifacts repositories get-iam-policy <repo> --location <region>` |
| MariaDB | serving read / batch write / admin | 데이터 소유자 + DBA | `SELECT CURRENT_USER(), DATABASE()` |
| jw-data-portal | uploader / admin | 포털 업무 소유자 | 로그인 후 역할과 미인가 화면 검증 |
| Secret 관리 | secret accessor / admin | 보안·플랫폼 | 리소스 IAM만 조회; 값 출력 금지 |

## 3. 신청

티켓에 다음 항목을 모두 적는다.

```text
신청자/소속:
업무 목적:
대상 환경: dev | test2 | production
대상 시스템/리소스:
요청 역할과 필요한 verb:
시작일/만료일:
운영 write 필요 여부:
Secret 접근 필요 여부와 사유:
승인자:
회수 담당자:
관련 변경/사건 티켓:
```

"관리자 전체"처럼 범위가 열린 신청은 반려한다. 필요한 리소스와 동작을 verb 단위로 적는다.

## 4. 승인과 발급

1. 서비스 소유자가 업무 필요성과 최소 범위를 검토한다.
2. 운영 write, DB admin, Secret 접근은 플랫폼/DBA/보안의 추가 승인을 받는다.
3. 발급자는 승인된 역할만 부여하고 시작·만료 시각을 기록한다.
4. 자격증명 전달은 승인된 비밀 전달 채널을 사용한다. 티켓·메일·Git·채팅에 값을 붙이지 않는다.
5. service account에는 소유 팀, 사용 workload, keyless 사용 여부, 회전 책임자를 기록한다.

## 5. 발급 후 검증

사용자는 실제 업무 대신 무해한 조회로 권한을 검증한다.

```bash
gcloud auth list --filter=status:ACTIVE --format='value(account)'
kubectl config current-context
kubectl auth can-i get deployments -n llmops
kubectl auth can-i get pods/log -n llmops
kubectl auth can-i patch deployments -n llmops
```

DB는 승인된 접속 경로에서 다음만 확인한다.

```sql
SELECT CURRENT_USER(), DATABASE();
SHOW GRANTS FOR CURRENT_USER();
```

Secret 검증은 키 이름과 참조 성공 여부만 확인한다. `kubectl get secret -o yaml`, base64 decode, env dump로 값을 출력하지 않는다.

## 6. 포털 사용자 온보딩

1. 승인된 Google 계정을 GenOS 사용자 정본에 등록한다.
2. 업무 역할에 따라 uploader 또는 admin 그룹을 부여한다.
3. 시크릿 창에서 Google 로그인, 홈 접근, 허용 메뉴를 검증한다.
4. 권한 없는 계정이 `/unauthorized`로 가는지 확인한다.
5. 역할 변경은 세션/디렉터리 캐시 만료 지연을 고려해 재로그인 후 검증한다.

개별 이메일을 fallback 계정으로 문서에 고정하지 않는다. 비상 접근은 break-glass 절차와 승인 기록으로 관리한다.

## 7. 정기 검토와 회수

- 월 1회 또는 조직 변경 시 사용자·service account·GKE RoleBinding·DB grant·Gitea/GitHub 팀을 검토한다.
- 만료일 도래, 퇴사·이동, 업무 종료, 90일 미사용, 보안 사건 발생 시 즉시 회수한다.
- 회수 순서: 활성 세션/토큰 폐기 → 역할 제거 → key/secret 회전 → 자동화 영향 확인 → 티켓 종료.
- 회수 뒤 `kubectl auth can-i`, repository access, DB login이 거부되는지 검증한다.
- service account는 먼저 대체 신원을 배포·검증한 뒤 구 신원을 회수한다.

## 8. Break-glass

- 운영 장애로 일반 승인이 불가능할 때만 사용한다.
- 시간 제한 역할, 2인 승인, 명시적 사건 번호를 요구한다.
- 사용 명령과 변경 리소스를 감사 로그에 남긴다.
- 장애 종료 즉시 권한 회수, 자격증명 회전, 사후 검토를 수행한다.

## 9. 인계 체크리스트

- [ ] 신청·승인 티켓과 만료일이 있다.
- [ ] 발급 범위가 verb·resource 단위다.
- [ ] 값이 문서·로그·Git에 없다.
- [ ] 무해한 조회로 권한을 검증했다.
- [ ] 회수 담당자와 정기 검토 주기가 정해졌다.
