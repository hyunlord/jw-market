# 배포·승격·롤백 런북

이 문서는 JW Market 백엔드 이미지를 test2에서 검증한 뒤 같은 digest를 운영에 승격하고, 실패 시 직전 이미지로 되돌리는 운영 절차다. 문서에 기록된 SHA·generation·replica 수를 사용하지 않는다. **실행 직전 Kubernetes API에서 조회한 값만 정본**이다.

## 1. 범위와 승인

- 대상 네임스페이스: `llmops`
- test2 Deployment: `jw-market-backend-api-test`
- 운영 Deployment: `jw-market-backend-api`
- 대상 컨테이너: `jw-market-backend-api`
- BFF(`portal-api-sse`)와 데이터 테이블은 이 절차의 대상이 아니다.
- test2 배포와 운영 승격은 각각 PL 승인 및 배타 창을 확인한다.
- `kubectl port-forward`는 사용하지 않는다. 검증 호출은 pod-local 또는 클러스터 내부 probe에서 수행한다.

## 2. 실행 전 live 좌표 고정

```bash
set -euo pipefail
NS=llmops
TEST2=jw-market-backend-api-test
PROD=jw-market-backend-api
CONTAINER=jw-market-backend-api

kubectl config current-context
kubectl -n "$NS" get deploy "$TEST2" "$PROD" -o wide
kubectl -n "$NS" get deploy "$TEST2" "$PROD" -o json > /tmp/jw-market-deploy-before.json

python3 - <<'PY'
import json
p = json.load(open('/tmp/jw-market-deploy-before.json'))
for d in p['items']:
    c = next(x for x in d['spec']['template']['spec']['containers']
             if x['name'] == 'jw-market-backend-api')
    env = {x['name']: x.get('value', '<valueFrom>') for x in c.get('env', [])}
    print({
        'name': d['metadata']['name'],
        'uid': d['metadata']['uid'],
        'resourceVersion': d['metadata']['resourceVersion'],
        'generation': d['metadata']['generation'],
        'replicas': d['spec'].get('replicas'),
        'image': c['image'],
        'APP_VERSION': env.get('APP_VERSION'),
        'EXTERNAL_PATH_PREFIX': env.get('EXTERNAL_PATH_PREFIX'),
    })
PY
```

중단 조건:

- kube context가 승인된 클러스터가 아니다.
- 다른 배포가 generation/resourceVersion을 변경했다.
- `EXTERNAL_PATH_PREFIX=/jw-market-backend-api`가 없거나 대상 컨테이너가 둘 이상이다.
- 후보 커밋이 최신 `develop`의 조상이 아니거나 clean tracked-only 테스트가 신규 실패한다.

## 3. 이미지 빌드와 digest 확인

이미지는 한 번만 빌드한다. 운영 승격 때 다시 빌드하지 않는다.

```bash
git fetch jw-private develop
git rev-parse HEAD
git rev-parse jw-private/develop
git merge-base --is-ancestor jw-private/develop HEAD

REGISTRY=asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01
TAG="jw-market-backend-api:$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%dT%H%M%SZ)"
IMAGE_TAG="$REGISTRY/$TAG"

docker build --platform linux/amd64 -f api/Dockerfile -t "$IMAGE_TAG" .
docker push "$IMAGE_TAG"
gcloud artifacts docker images describe "$IMAGE_TAG" --format='value(image_summary.digest)'
```

출력 digest를 `DIGEST`에 넣고, 이후에는 항상 immutable 좌표를 사용한다.

```bash
DIGEST='sha256:<live-output>'
CANDIDATE="$REGISTRY/jw-market-backend-api@$DIGEST"
APP_VERSION="$(git rev-parse HEAD)"
```

## 4. test2 배포

배포 직전에 2절의 JSON을 다시 수집해 resourceVersion과 generation이 바뀌지 않았는지 확인한다. 아래 함수는 컨테이너 index를 구조적으로 찾고, 현재 `resourceVersion`을 JSON Patch `test`로 검증한 뒤 image와 env를 한 요청에 바꾼다.

```bash
deploy_digest() {
  local deploy="$1" image="$2" version="$3" before patch
  before="$(mktemp)"
  kubectl -n "$NS" get deploy "$deploy" -o json > "$before"
  patch="$(python3 - "$before" "$CONTAINER" "$image" "$version" <<'PY'
import json, sys
path, name, image, version = sys.argv[1:]
d = json.load(open(path))
containers = d['spec']['template']['spec']['containers']
i = next(i for i, c in enumerate(containers) if c['name'] == name)
env = list(containers[i].get('env', []))
by_name = {x['name']: j for j, x in enumerate(env)}
for key, value in [('APP_VERSION', version),
                   ('EXTERNAL_PATH_PREFIX', '/jw-market-backend-api')]:
    item = {'name': key, 'value': value}
    if key in by_name:
        env[by_name[key]] = item
    else:
        env.append(item)
print(json.dumps([
    {'op': 'test', 'path': '/metadata/resourceVersion',
     'value': d['metadata']['resourceVersion']},
    {'op': 'replace', 'path': f'/spec/template/spec/containers/{i}/image',
     'value': image},
    {'op': 'replace' if 'env' in containers[i] else 'add',
     'path': f'/spec/template/spec/containers/{i}/env', 'value': env},
]))
PY
)"
  kubectl -n "$NS" patch deploy "$deploy" --type=json -p "$patch"
  rm -f "$before"
}

deploy_digest "$TEST2" "$CANDIDATE" "$APP_VERSION"
kubectl -n "$NS" rollout status "deploy/$TEST2" --timeout=420s
```

모든 test2 pod를 검사한다. 첫 pod만 고르는 명령(`head -1`)은 금지한다.

```bash
kubectl -n "$NS" get pods -l app="$TEST2" -o json > /tmp/test2-pods.json
python3 - "$CANDIDATE" <<'PY'
import json, sys
p = json.load(open('/tmp/test2-pods.json'))['items']
assert p, 'empty pod population'
want = sys.argv[1].split('@', 1)[1]
for pod in p:
    cs = next(x for x in pod['status']['containerStatuses']
              if x['name'] == 'jw-market-backend-api')
    ready = cs['ready'] and cs['restartCount'] == 0 and want in cs['imageID']
    print(pod['metadata']['name'], cs['imageID'], cs['restartCount'], ready)
    assert ready
PY
```

검증 순서:

1. `/api/health`와 신규 기능 스모크.
2. `pipeline/scripts/gates/release_acceptance.py`의 해당 subcommand 전건.
3. canonical JSON 골든과 기능별 독립 정본 대조.
4. 모든 pod의 `Traceback|ERROR|5xx` 건수를 세어 0을 요구한다.
5. test2가 도중에 다른 digest로 교체되면 덮어쓰지 않고 중단한다.

## 5. 운영 승격

PL 승인 뒤 **test2에서 검증한 `$CANDIDATE` 그대로** 운영에 적용한다. 승격 직전 운영 JSON을 새로 저장해 롤백 좌표를 남긴다.

```bash
kubectl -n "$NS" get deploy "$PROD" -o json > /tmp/jw-market-prod-before.json
deploy_digest "$PROD" "$CANDIDATE" "$APP_VERSION"
kubectl -n "$NS" rollout status "deploy/$PROD" --timeout=420s
```

운영에서도 test2와 동일하게 전체 pod의 Ready, restart 0, `imageID` digest, `APP_VERSION`, strict log 0을 검사한다. 성공한 승격은 되돌리지 않는다.

## 6. 이미지 롤백

기능 게이트, rollout 또는 strict log가 실패한 경우에만 수행한다. `/tmp/jw-market-prod-before.json`에서 직전 image와 `APP_VERSION`을 구조적으로 읽는다.

```bash
read -r PREV_IMAGE PREV_VERSION <<EOF
$(python3 - <<'PY'
import json
d = json.load(open('/tmp/jw-market-prod-before.json'))
c = next(x for x in d['spec']['template']['spec']['containers']
         if x['name'] == 'jw-market-backend-api')
env = {x['name']: x.get('value', '') for x in c.get('env', [])}
print(c['image'], env.get('APP_VERSION', ''))
PY
)
EOF

deploy_digest "$PROD" "$PREV_IMAGE" "$PREV_VERSION"
kubectl -n "$NS" rollout status "deploy/$PROD" --timeout=420s
```

롤백 후에도 전체 pod identity·health·strict log를 다시 검사하고 사건 기록에 후보/직전 digest, generation, 시작·종료 시각, 원인을 남긴다.

## 7. 데이터 세대 롤백은 별도 절차

이미지 롤백과 mart 롤백을 섞지 않는다. mart 롤백은 ledger에 기록된 **전체 promotion 세트**만 지원하며, 특정 소스만 되돌리는 부분 롤백은 지원하지 않는다.

```bash
python3 -m pipeline.scripts.rollback --to latest-good --dry-run \
  --target-db "$DB_NAME" --actor "$USER" --reason '<ticket-or-incident>'

# 출력의 대상 세대, 비어 있지 않은 백업, row count와 digest를 승인한 뒤에만:
python3 -m pipeline.scripts.rollback --to <promotion-run-id> --yes \
  --target-db "$DB_NAME" --actor "$USER" --reason '<ticket-or-incident>'
```

`--yes` 없는 실행은 계획만 출력한다. 데이터 RENAME/복구는 별도 PL 승인과 백업·복구 정책을 따른다.

## 8. 인계 기록

각 배포 기록에는 다음을 남긴다.

- git commit, image tag, immutable digest
- test2/운영 Deployment UID·generation·resourceVersion
- 전체 pod imageID·restart 수
- 적용한 acceptance gate와 checked/population/failures/exit code
- 롤백 필요 시 직전 image·APP_VERSION과 사건 사유

고정 좌표를 이 문서에 다시 쓰지 않는다. 실행 증거는 라운드별 audit에 보관한다.

## 9. code-serving-235 브리지 배포

`code-serving-235`(`chat/wf301-vdb-bridge`)는 위 backend-api 절차의 대상이 아니다. 이 서비스는 GenOS code-serving 등록과 승인을 거쳐 배포하며, 현재 singleton `Recreate` 구성에서는 stop 승인부터 새 pod 준비까지 파일 업로드·검색이 완전히 중단된다.

### 9.1 배포 전 앵커

다음 항목을 **같은 시점의 Kubernetes JSON**에서 함께 보존한다. 이미지와 commit만 기록하면 롤백 좌표가 불완전하다.

- Deployment generation, strategy, replicas와 전체 pod imageID/restart
- 컨테이너 image, `COMMIT_HASH`, env 전체(Secret 값은 저장하지 않고 `valueFrom` 참조 유지)
- `volumeMounts`, `volumes`, PVC/ConfigMap 참조
- 현재 이미지 digest에 대응하는 Artifact Registry 태그의 존재
- GenOS의 활성 deployment ID, DockerImage ID, instance type, replicas

후보와 직전 이미지는 **stop 전에** GenOS DockerImage로 모두 등록하고 `COMPLETED` 상태와 실제 registry digest를 대조한다. 감사 산출물에는 Secret 값이나 광범위한 승인자 목록을 넣지 않는다.

### 9.2 승인과 배포

1. GitHub 원격 SHA와 로컬 SHA, 최신 기준 브랜치를 다시 대조한다.
2. 실행 정본 저장소의 commit과 exact-source 이미지 revision을 대조한다.
3. `/serving/code/235/stop` 요청의 `approval_id`를 `/approval/approve`로 승인한다.
4. 중지 완료 뒤 `/serving/code/235/deploy`에 commit, DockerImage ID, instance type, replicas를 제출하고 새 `approval_id`를 승인한다.
5. rollout 뒤 pod imageID가 후보 digest와 정확히 같고 Ready 1/1, restart 0인지 확인한다.

2026-07-22 실측에서 stop/deploy 승인 API 처리는 각각 약 3.5~4.7초였지만, 운영 SLA로 간주하지 않는다. 승인 요청·승인·rollout 시작과 종료 시각을 매 실행마다 별도로 기록한다.

### 9.3 pod-template 동등성 hard gate

GenOS 배포가 새 Deployment를 생성할 때 기존 수동 env와 PVC mount를 승계한다는 보장은 없다. 2026-07-22에는 기존 42개 env가 6개로 줄고 `/nfs-root` PVC mount가 사라졌다. 그 결과 `/documents`는 `DB_PASSWORD is not configured`, 업로드는 `/nfs-root` 권한 오류로 실패했다.

기능 호출 전에 후보 Deployment를 앵커와 구조적으로 대조한다.

- 컨테이너 population과 이름
- env 이름 population 및 각 `value`/`valueFrom` 형태
- volume/volumeMount population, 이름, mountPath, PVC/ConfigMap 참조
- service selector와 container port

하나라도 다르면 후보 기능을 통과시키지 않는다. GenOS 서비스 정의에서 설정을 복원할 수 없으면 후보를 중지하고 직전 이미지를 승인 재배포한 뒤, 앵커의 env와 mount를 이름 기준으로 복원한다. Secret은 평문 env로 바꾸지 않고 기존 `secretKeyRef`를 유지한다.

### 9.4 라이브 계약 게이트

pod-template 동등성 통과 후 고유 세션으로 다음을 순서대로 검증한다.

1. 파일 업로드 성공
2. `GET /documents`의 `document_id`·`temp_document_id`
3. 반환된 ID를 사용한 `POST /documents/delete`
4. 재조회 잔존 0
5. OpenAPI의 additive 필드와 기존 source projection 불변
6. CSV 인코딩·구분자 조합, 기존 PDF/PPTX/DOCX/XLSX 스모크
7. chat 파일 질답, 현재 pod 전건 strict 로그

`checked`와 `population`을 분리하고 population 0은 실패로 처리한다. SQL 전용 문서는 벡터 `result_count=0`일 수 있으므로 `sql_available`과 SQL query 결과로 판정한다. 테스트 문서는 성공·실패와 무관하게 삭제하고 재조회 잔존 0을 요구한다.

### 9.5 수동 롤백

mandatory gate 실패 시 후보 deployment를 stop 승인하고, 9.1에서 등록한 직전 DockerImage ID와 commit으로 deploy 승인한다. 이미지 복귀 뒤에도 env·Secret 참조·PVC mount를 앵커와 대조하고, 누락 시 이름 기준으로 복원한다. 마지막으로 다음을 모두 확인한다.

- 직전 immutable imageID, Ready 1/1, restart 0
- env/volume/volumeMount 동등성
- `/documents`와 `/search` 정상 응답
- chat 파일 질답 1회와 테스트 문서 잔존 0
- 현재 pod strict 로그 실패 0

자동원복은 없으며, 승인 지연과 pod-template 비승계가 복구 시간을 지배한다. 후보 실패를 성공으로 기록하지 않고, 실패 원인과 수동 복원 항목을 사건 기록에 남긴다.
