# Executor 개발자 연동 문서

이 디렉토리는 Agent/BFF 연동에 필요한
문서 모음이며, 모든 운영 API에 개별 Markdown이 있는 것은 아니다.

## 실행 API

| API | 가이드 |
| --- | --- |
| `POST /api/v1/executions` | [제출](post-executions.md) |
| `GET /api/v1/executions/{execution_id}` | [상태·오류·정리 상태](get-execution.md) |
| `POST /api/v1/executions/{execution_id}/cancel` | [취소](post-execution-cancel.md) |
| `POST /api/v1/executions/{execution_id}/retry` | [SINGLE 재시도](post-execution-retry.md) |
| `POST /api/v1/executions/{execution_id}/operations` | [MULTI 후속 Operation](post-execution-operations.md) |
| `POST /api/v1/executions/{execution_id}/finalize` | [MULTI 최종화](post-execution-finalize.md) |
| `GET /api/v1/executions/{execution_id}/result` | [통합 결과 인덱스](get-execution-result.md) |
| `GET /api/v1/executions/{execution_id}/diagnostics` | [진단 이력](execution-diagnostics.md) |
| `GET /api/v1/artifacts/{artifact_id}/content` | [현재 파일 다운로드](get-artifact-content.md) |
| `POST /api/v1/runtime-targets/{target_id}/purge` | [서버 등록 제거, REST 전용](post-runtime-target-purge.md) |

[Execution 전용 ReDoc HTML](execution-api-redoc.html)은 현재 Execution 태그의
22개 경로와 110개 component schema를 포함한다. Artifact 작성/목록/상세,
Operation·Step·Attempt·Event·Notebook 조회의 전체 필드도 이 문서에서 확인한다.
Runtime 관리와 maintenance 전체 계약은 서비스 `/docs`, `/redoc` 또는
[전체 REST 가이드](../docs/rest-api.md)를 사용한다.

## 이벤트와 파일

- [Redis 이벤트 1.0](redis-execution-events.md): 7개 envelope 필드와 6개 이벤트 유형.
- [Agent 소비 가이드](agent-execution-event-consumer-guide.md): 순서·중복·누락 복구,
  SINGLE/MULTI wake-up 경계와 실패 처리.
- [Manifest 설명](step-result-manifest.md) / [JSON Schema](step-result-manifest.schema.json):
  코드·출력 원본을 읽는 파일 계약.
- [현재 시간 제한](execution-timeouts.md): 요청값의 단위, 범위, 대기·retry·만료 처리.

## 반드시 구분할 사항

1. 기본 커널 프로파일은 `default`, `3102311`이다. 배포 설정이 다르면 실제
   `RUNTIME_ALLOWED_PROFILES`와 서버의 지원 프로파일을 확인한다.
2. PATH 입력은 `INPUT_STORAGE_ROOT` 기준이다. 미지정 시에만 `SHARED_STORAGE_ROOT`를
   사용한다. source/output/result ref는 Executor 결과 루트 `SHARED_STORAGE_ROOT`
   기준이며 Agent에 마운트된 해당 결과 루트에 결합한다.
3. Jupyter의 notebook/report/artifact 파일은 Jupyter PV에 있다. Step 결과 원본은
   Agent/Executor 공유 PV에 있다. Artifact의 경로를 결과 루트에 결합하지 않는다.
4. `completed` 이벤트는 성공뿐 아니라 실패·취소도 나타낸다. status를 확인한다.
5. `CANCELLED`는 원격 커널 정리 성공을 보증하지 않는다. 상세의 recovery를 본다.
6. 노트북은 최초 저장 시부터 등록되고 실행 중·실패 후에도 다운로드할 수 있다.
   `AVAILABLE`은 분석 성공이나 모든 출력의 반영 완료를 뜻하지 않는다.
7. 전용 코드 계산시간·대기시간 누적 필드는 제공하지 않는다. Agent의 생존 신호를
   주기적으로 받거나 Agent 장애만을 이유로 실행을 자동 취소하지 않는다.

## 예시 사용 안내

예시의 사용자 ID, 실행 ID, 체크섬은 실제 요청 대상에 맞게 지정한다.
PATH 요청의 파일은 입력 루트 아래에 작성하고, 실행에 사용할 Runtime을 등록한다.
