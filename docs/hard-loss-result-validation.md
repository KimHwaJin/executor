# 강제 종료·오래된 Worker 복귀 시 결과 정합성 검증

## 기준과 복귀 지점

- 시작 기준: `main`의 `8c50baa7d69b1ecc42071f9e56e3c2fe960396b5`.
- 작업 브랜치: `feature/hard-loss-result-validation`.
- Jupyter 커스텀 저장 방식과 노트북 `0644` 정책을 유지한다.
- Contents API 비교는 별도 `feature/jupyter-contents-api-evaluation`에 보존한다.
- 복귀 시 작업 브랜치를 보존하고 위 기준 커밋의 main으로 전환한다.
  DB/Redis 초기화, 원격 이력 재작성, 사용자 파일 삭제는 하지 않는다.

## 계획 — 실행 전

1. 현재 로컬 Jupyter 이미지의 권한 구현을 읽기 전용으로 확인한다.
2. 최신 Executor 코드와 Jupyter 확장을 적용한 테스트 전용 이미지를 만든다.
   일반 서비스와 이미지 태그를 변경하지 않는다.
3. UUID Compose 프로젝트의 별도 DB, Redis, 공유 결과 볼륨, Jupyter PV에서
   SINGLE/MULTI × basic/ml의 텍스트·PNG 출력 중 SIGKILL을 검증한다.
4. Worker를 일시 정지해 lease를 만료시키고, 다른 Worker의 정리 후 재개하여
   이전 소유자의 뒤늦은 결과 반영을 검증한다. 자동 코드 재실행을 가정하지 않는다.
5. REST/DB/Redis 이벤트, 완료 Step의 manifest/checksum, 미완료 Step의
   참조 유무, 노트북 최신성 표시, 커널 정리를 비교한다.
6. 발견한 결함은 최소 범위로 수정하고 회귀 테스트·실제 Docker 재검증 후 기록한다.

강제 종료에서 아직 봉인·DB commit되지 않은 출력의 자동 복구를 약속하지 않는다.
중간 파일은 공개 결과가 아니며, 결과가 없을 때 완료로 표시해서는 안 된다.
파일 봉인과 DB commit 사이의 장애, DB·Redis·스토리지 장애 주입은 이번 결과에
실제 수행 범위를 구분하여 기록한다. 테스트가 없는 경우 통과로 간주하지 않는다.

## 환경 확인

현재 일반 로컬 `executor-jupyter-1`에는 `os.fchmod(..., 0o644)`가 없는
이전 코드가 로드되어 있다. 소스의 수정이 실행 중인 이미지에 자동 반영된 것이 아니다.
일반 서비스의 이미지 갱신·재시작은 이번 격리 검증과 별개의 배포 작업이다.

## 결과

### 재현과 수정

수정 전 `kill / SINGLE / basic`에서 다음 문제를 재현했다.

- Execution은 `FAILED / LEASE_EXPIRED`, 커널 정리는 성공했다.
- Step 0 결과는 정상 보존됐고, Step 1의 아직 봉인되지 않은 결과는 공개되지 않았다.
- 그런데 노트북에는 Step 0 출력만 있는데 `notebook_projection.status=SUCCEEDED`
  및 이전 `projected_at`이 남아 있었다. JSON 재현 보고서:
  `test-results/hard-loss-baseline.json`.

`94bc943`에서 lease 만료 복구 트랜잭션 안에 다음을 보완했다.

- 노트북 경로가 있고 기존 구체적 projection 실패가 없다면 `FAILED`로 표시하고
  `projected_at=null`, Worker lease 만료로 최신성을 확인할 수 없다는 사유를 기록한다.
- `NOTEBOOK_NOT_REFRESHED / NOTEBOOK_LEASE_EXPIRED` 진단을 기존 테이블에 기록한다.
  Execution/Attempt/Operation 식별자, 진단 작성 시점의 새 fence, 감사 필드를 보존한다.
- 노트북 상태, fence 갱신, terminal 이벤트, 진단은 같은 DB 트랜잭션이다.
  진단 INSERT가 실패하면 함께 롤백되며 다음 복구 시도에서 처리할 수 있다.
  commit 후에만 식별자 포함 안전 로그를 남긴다.
- 이전 notebook 실패 사유와 실행의 원래 timeout/lease failure는 덮어쓰지 않는다.
  노트북 write attempt_count를 늘리거나 원격 파일을 쓰지 않는다.

SIGKILL에서는 이미 봉인·등록된 Step 결과만 공개된다. 중단 중이던 Step의
`result_ref=null`은 자동 복원할 결과가 있다는 뜻도, 빈 출력으로 성공했다는 뜻도 아니다.
일시 정지 후 돌아온 Worker가 파일을 뒤늦게 봉인해도 새 fence에서 DB 참조를
연결할 권한은 없다. 정상 취소·SIGTERM의 협조적 부분 결과 봉인 경로와 구분한다.

### 검증 기록

- 품질 gate: Ruff, 포맷(383 Python 파일), ty 통과.
- 일반 회귀 **450 passed**, PostgreSQL **24 passed**, Redis **10 passed**.
  macOS에서 건너뛴 POSIX identity 테스트 1개는 Linux Docker에서 별도 통과했다.
- DB 오류 주입: diagnostic INSERT 실패 시 상태/fence/event 롤백 후 정상 재시도 확인.
- 실제 PostgreSQL: 8개 Worker 동시 복구에서 단 한 번의 fence/terminal event/진단.
- 완료/미완료 결과 파일, Redis 7필드 envelope와 REST history, Step/Attempt 참조,
  커널 정리, 노트북 상태를 비교했다.

2026-08-31 실제 Docker 검증: **16개 조합 모두 PASS**.
테스트 PASS는 중단된 실행을 성공으로 표시했다는 뜻이 아니다. SIGKILL/pause는
`FAILED / LEASE_EXPIRED`, 취소는 `CANCELLED`, SIGTERM은 `FAILED / WORKER_SHUTDOWN`이다.

| 커널 | 모드 | SIGKILL | pause → 만료 → 복귀 | 취소 | SIGTERM |
|---|---|---|---|---|---|
| basic | SINGLE | PASS | PASS | PASS | PASS |
| basic | MULTI | PASS | PASS | PASS | PASS |
| ml | SINGLE | PASS | PASS | PASS | PASS |
| ml | MULTI | PASS | PASS | PASS | PASS |

- 모든 케이스에서 공개 이벤트 8개, `event_sequence=1..8`, 실제 Redis/DB/REST 일치.
  at-least-once의 동일 메시지 재발행은 허용하되 내용이 다르면 실패시키는 검사다.
- SIGKILL/pause: 완료된 Step 0만 manifest 공개, Step 1 참조 없음, Step 2 미실행.
- 취소/SIGTERM: Step 0 완전 결과와 Step 1 불완전 텍스트·PNG를 각각 공개.
- 모든 케이스에서 노트북 최신성 `FAILED`, 명시적 사유와 진단, 커널 잔존 0개.
- pause 복귀 후 추가 12초 동안 결과·이벤트 및 DB 조회 스냅샷 변경 없음.
- 취소/SIGTERM의 실제 노트북 8개는 UID 65534로 `0644`, 읽기·HTML 렌더링 통과.
  초기 생성/기존 600 파일 재작성의 별도 identity 테스트도 두 스택에서 통과했다.

원본 JSON과 SHA-256:

- `test-results/hard-loss-baseline.json` (수정 전 실패)
  `50f3e63634d5352253a87149865375a559e33c4765a6e93964d26b78cdfe9f37`
- `test-results/hard-loss-results.json` (강제 종료/복귀 8개)
  `32fe86badd8bb04641d5fa7d3f29c16b9ffb1135da4d0f4a04a5f39d4f034cf6`
- `test-results/hard-loss-cooperative-regression.json` (협조적 중단 8개)
  `f66f2cf8ab2bc30e4fb9031839ea8bb0e0a117077acc9eff579b3c5ea6d36520`
- `test-results/hard-loss-final-resume.json` (최종 pause/SINGLE/basic 재검증)
  `e4a8647c3d864777daf98111ef70f4c696f972ea7e84e7c6fb3b46eb5482ecb0`

마지막 재검증은 `8bd766f`에서 통과했다. Worker 복귀 후 Execution state와
노트북 projection 상태도 직접 재조회하여 불변임을 확인했고, 실제 노트북의
UID 65534 읽기·렌더링도 함께 통과했다. 모든 격리 프로젝트의 컨테이너·볼륨은
정리했으며 기존 일반 로컬 서비스 4개와 사용자 데이터는 변경하지 않았다.

첫 hard-loss 보고서의 `git_commit=038af7e`는 실행 시작 때의 HEAD다.
실제로 빌드한 src는 당시 미커밋 상태였고 이후 `94bc943`에 그대로 확정했다.
협조적 중단 보고서는 `94bc943`에서 실행했다. 코드 복귀/재현에는 이 차이를 참고한다.
개별 `elapsed_seconds`는 기본 상태·파일 검증까지의 관측값이며 pause의 추가 12초
관측과 스택 준비/정리를 포함한 전체 시간 또는 서비스 SLO가 아니다.

### 재현 명령

```bash
uv run python scripts/docker_interrupted_result_e2e.py \
  --actions kill pause --report test-results/hard-loss-results.json
uv run python scripts/docker_interrupted_result_e2e.py \
  --actions cancel shutdown --report test-results/hard-loss-cooperative-regression.json
uv run python scripts/quality_gate.py --integration
```

하나만 진단하려면 `--profiles basic --modes SINGLE`을 추가한다.
중단 대상은 스크립트가 새로 만드는 UUID Compose 프로젝트의 두 Executor만이다.
보고서는 케이스 단위로 저장하고, 실패한 경우에도 수집된 상태·파일·이벤트 증거를 남긴다.
기본 종료 시 테스트 컨테이너·볼륨은 삭제하며, JSON만 `test-results/`에 남는다.
원본 테스트 노트북·DB는 이때 삭제되어 복구용으로 보관되지 않는다.
`--keep-stack`은 조사 용도이며 실패 지점에 따라 Worker가 pause 상태일 수 있다.
정리 시 해당 project와 두 Compose 파일을 명시하여 unpause 후 down한다.

### 배포 영향과 한계

- REST/MCP/Redis 스키마 및 Alembic 변경 없음. Executor 이미지에 복구 수정 반영 필요.
- Jupyter의 `0644` 수정은 Jupyter 이미지 재빌드 또는 확장 재설치 후 서버 재시작 필요.
  기존 일반 로컬 서비스는 이번 테스트에서 갱신하지 않는다.
- 별도 Linux UID 1000 writer와 UID 65534 reader로 최초·재작성·기존 0600 파일 교체를
  검사한다. `nbformat` 검증과 nbconvert HTML 렌더링이며 실제 nbviewer HTTP 배포 검증은 아니다.
- 기존 디렉토리의 접근 권한은 바꾸지 않는다. 부모 디렉토리 탐색 권한은 운영 책임이다.
- 기존 600 파일의 일괄 권한 변경, notebook 재생성, private partial/orphan 파일 자동 복원 없음.
- pause 해제 후 관측은 최소 12초(테스트 heartbeat 5초의 두 주기 이상)다.
  이는 모든 타이밍의 stale write가 불가능하다는 증명이 아니며 기존 fence 회귀와 함께 본다.
- 실제 DB 장기 중단, Redis 장기 중단과 Outbox catch-up, PV 단절/디스크 고갈,
  파일 봉인과 DB commit 사이의 정확한 강제 종료, 1~5일 soak는 이 매트릭스에 포함하지 않는다.
  DB/스토리지 자체가 불능이면 영속 진단을 무조건 남길 수 없다는 한계도 유지한다.
