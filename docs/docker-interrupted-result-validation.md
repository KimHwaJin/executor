# 실제 취소·정상 종료 시 부분 결과 검증

## 범위

2026-08-31, 실제 Docker 프로세스로 다음 조합을 검증했다.

- Executor 2개, PostgreSQL, Redis, Jupyter 1개를 UUID Compose 프로젝트로 격리.
- SINGLE/MULTI × basic(Python 3.11)/ml(Python 3.12) × 사용자 취소/SIGTERM.
- Step 0: 텍스트 출력 후 정상 완료.
- Step 1: Matplotlib 막대그래프 PNG와 텍스트 출력 후 180초 대기.
- Step 2: 실행되면 안 되는 후속 코드.

Step 1의 텍스트와 PNG가 공유 PV에 실제 저장된 것을 확인한 후 중단한다.
임의 sleep 후 취소하는 테스트가 아니다. `.partial/.state.json`을 읽는 것은
테스트의 중단 시점 동기화에만 사용하며, Agent에 공개하는 계약이 아니다.
결과 검증은 DB가 가리키는 봉인된 manifest만 사용한다.

정상 종료는 해당 Attempt를 소유한 Executor에 `docker compose stop`을 실행한다.
이 테스트의 drain 유예는 5초, Docker stop 제한은 45초다. 180초 작업이 유예 내
끝나지 않으므로 Worker는 협조적 중단 후 `WORKER_SHUTDOWN`으로 처리한다.
컨테이너 exit code 0, OOM 아님, 실행 중 아님을 확인한다. SIGKILL 테스트가 아니다.

## 실제 비교 항목

1. REST submit/cancel 응답과 PostgreSQL terminal 상태.
2. Execution/Operation result, Step 상세, StepAttempt 결과의 동일한 참조.
3. DB/REST 이벤트와 실제 `executor.events` Redis 메시지의 7필드 envelope.
4. Execution별 연속 sequence, 누락 없는 발행, 중복 발생 시 동일 내용 여부.
5. Step·Operation 이벤트의 result_ref와 manifest 경로·byte 크기·SHA-256·complete.
6. manifest identity의 Execution/Step/Attempt/fence 및 실제 source/출력 파일의
   checksum/크기. 텍스트 중단 전 marker와 PNG signature도 검증.
7. 완료 Step `complete=true`, 중단 Step `complete=false`, 미실행 Step 참조 없음.
8. kernel 삭제, Execution/Attempt lease 해제 및 cleanup `SUCCEEDED`.
9. 노트북 API의 이전 반영본과 ‘최신 출력 미반영’ 상태/진단을 함께 확인.

모든 케이스에서 PNG는 10,459 bytes였다. 각 Execution에서 공개 이벤트 8개가
`event_sequence=1..8`로 기록·발행됐고 terminal 이벤트 중복이나 커널 누수는 없었다.
원본 이미지/텍스트는 Redis나 DB에 넣지 않는다.

## 최종 검증 결과

2026-08-31 15:05:56–15:07:19 KST, 최종 스크립트 재실행 기준이다.

| 환경 | 모드 | 사용자 취소 | 정상 종료 |
|---|---|---|---|
| basic | SINGLE | PASS / CANCELLED / 5.16초 | PASS / WORKER_SHUTDOWN / 9.87초 |
| basic | MULTI | PASS / CANCELLED / 3.43초 | PASS / WORKER_SHUTDOWN / 9.95초 |
| ml | SINGLE | PASS / CANCELLED / 4.84초 | PASS / WORKER_SHUTDOWN / 10.00초 |
| ml | MULTI | PASS / CANCELLED / 4.83초 | PASS / WORKER_SHUTDOWN / 9.84초 |

시간은 submit부터 결과·파일·이벤트·커널 검증까지의 테스트 케이스 소요시간이다.
취소 API 자체의 지연 시간이나 부하 환경 SLO로 해석하지 않는다.

- 전체 일반 회귀: **439 passed**, PostgreSQL/Redis 통합: **34 passed**.
- 최종 Docker 실제 실행: **8/8 passed**. 앞선 수정 후 실행도 8/8 통과했다.
- Ruff, 포맷 검사, ty 통과.
- 노트북 미반영 상태와 독립 진단 확인. 앞선 실패 사유 보존, 봉인 실패,
  DB 참조 저장 실패/timeout 및 stale lease 거부도 회귀 테스트로 확인했다.
- final JSON: `test-results/docker-interrupted-results-final.json`.
- 대표 취소 ID: `2850aef2-485d-4cb7-ba72-6feceaafbdff`.
- 대표 MULTI 종료 ID: `fa3b38fd-95ad-4cdf-8722-ba491e2e8e0d`.

테스트 전용 컨테이너·볼륨은 제거했다. 기존 로컬 서비스 네 개는 그대로이며,
이번 feature 수정이 실행 중인 일반 로컬 Executor에 자동 반영된 것은 아니다.

## 발견한 결함과 수정

첫 8조합 실행에서 부분 파일·이벤트·DB·커널 정합성은 통과했지만, 노트북을 별도로
열어보니 Step 1 출력이 없는데 `notebook_projection.status=SUCCEEDED`가 남았다.
이는 Step 0을 마지막으로 반영했을 때의 성공 표시였다.

협조적 중단은 정상 projection 경로를 지나지 않는다. 다음과 같이 수정했다.

- 기존 execution lease/fence를 검증하는 2초 제한 DB 작업에서 부분 참조를 연결하고,
  노트북의 이전 성공 표시를 무효화한다.
- `workspace.notebook_projection.status=FAILED`, `projected_at=null` 및
  ‘Step 중단 후 최신 노트북 미반영’ 사유를 남긴다.
- `NOTEBOOK_NOT_REFRESHED / NOTEBOOK_INTERRUPTED / NOTEBOOK / EXECUTOR`
  진단을 기존 진단 API와 안전 로그로 제공한다. 진단 DB 쓰기도 기존 2초 제한이다.
- 봉인 실패로 result_ref가 없어도 DB를 쓸 수 있으면 노트북 상태를 갱신한다.
- 이미 구체적인 노트북 실패가 있으면 그 사유를 덮어쓰지 않는다.
- 원래 `CANCELLED`/`WORKER_SHUTDOWN` 상태와 retry 정책을 변경하지 않는다.
- 원격 notebook 쓰기를 추가하지 않으므로 실제 write attempt_count는 증가하지 않는다.

중단 중 노트북 자동 재생성을 구현한 것이 아니다. 취소/정상 종료 후 노트북은
이전 반영본일 수 있으며, Agent는 공유 PV result_ref에서 부분 출력을 읽는다.
DB 장애, lease 상실, 프로세스 강제 종료에서는 이 상태/진단 기록도 보장할 수 없다.

## 스키마와 배포 영향

- REST/MCP 요청·응답 필드 및 Redis 이벤트 스키마 변경 없음.
- 기존 자유 문자열 `diagnostic.code`에 `NOTEBOOK_NOT_REFRESHED` 값 추가.
- 기존 notebook projection 상태/사유 값의 정확성 개선.
- DB migration 없음. head `0003`, 계약 버전 `1.0` 유지.
- Jupyter 커스텀 API/이미지 코드 변경 없음. Executor 이미지만 수정 대상으로 빌드.
- 기존 로컬 Executor/Jupyter/PostgreSQL/Redis 및 사용자 데이터는 변경하지 않음.

## 실행 방법

Docker와 로컬 `executor-service:local`, `executor-jupyter:local` 이미지가 필요하다.
Executor 의존성이 들어 있는 로컬 이미지를 바탕으로 현재 src/migrations만 교체해
테스트 이미지를 빌드한다. 의존성이 바뀌었다면 기본 이미지를 먼저 최신화해야 한다.

```bash
uv run python scripts/docker_interrupted_result_e2e.py \
  --report test-results/docker-interrupted-results-final.json
```

새 프로젝트명과 host port는 매번 생성한다. DB/Redis는 호스트에 포트를 노출하지 않고
프로젝트 전용 네트워크·named volume을 사용한다. 등록 토큰은 메모리에서 생성하며
리포트에 기록하지 않는다. 일반 `docker compose up/down`을 호출하지 않는다.

기본값은 종료 후 테스트 컨테이너·전용 볼륨을 삭제한다. 정리 실패는 PASS로 숨기지
않고 보고서에 남긴다. `--keep-stack`이면 테스트 스택을 남겨 조사할 수 있다.
정리할 때는 보고서의 정확한 project 이름과 아래 두 파일을 모두 지정한다.

```bash
docker compose --project-name <report.project> \
  --file compose.worker-failover.yaml \
  --file compose.interrupted-results.yaml down --volumes --timeout 45
```

JSON 리포트는 케이스마다 원자적으로 갱신하며 API/이벤트/DB 상태, manifest 참조,
검증한 PNG 크기, 텍스트, 노트북 요약, 진단을 담는다. `test-results/`는 git 제외다.
기본 정리 후 원본 notebook/출력/DB는 남지 않으므로 테스트 ID를 일반 로컬 서비스에서
조회할 수 없다. 삭제된 테스트 볼륨은 복구용으로 보관하지 않는다.

## 판정의 한계와 다음 검증

이 검증은 실제 실행 중 사용자 취소와 협조적 종료의 결과 정합성 검증이다.
부하/SLO 측정, 1~5일 soak, 모든 종료 시점의 보장이 아니다.

다음은 SIGKILL/lease 만료/다른 Worker takeover와 DB·Redis·스토리지 장애를
현재 부분 결과 계약 기준으로 검증한다. 특히 공유 파일 봉인과 DB commit 사이에
강제 종료되면 참조 없는 파일이 남을 수 있으며 이번 작업은 이를 자동 복구하지 않는다.
노트북 재생성은 별도 보류 항목으로 유지한다.
