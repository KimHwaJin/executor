# Executor 개발자 연동 문서

검토 기준: 2026-09-21 현재 저장소 구현. 이 디렉토리는 Agent/BFF 연동에 필요한
문서 모음이며, 모든 운영 API에 개별 Markdown이 있는 것은 아니다.
논의 중인 제안은 공개 API 계약에 포함하지 않는다.

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
7. 소요시간 세분화·추가 DB 필드는 아직 도입하지 않았다. Agent heartbeat 정책도
   채택하지 않았다. [설계 논의 기록](../docs/execution-time-contract-review.md)은
   구현된 계약이 아니다.

## 재생성 및 검증

ReDoc에는 런타임 스크립트와 스키마가 포함되어 있다. 다음 명령은 기존 HTML의
ReDoc 스크립트를 재사용해 현재 코드에서 스키마를 재생성한다.

```bash
uv run python scripts/generate_execution_redoc.py --redoc-js dev_docs/execution-api-redoc.html
```

API 입력 모델의 검증 통과와 실제 실행 가능 여부는 다르다. 예시의 사용자 ID,
실행 ID, checksum, 파일 존재, Runtime 등록은 환경에 맞게 준비해야 한다.
스키마 검증은 실제 서비스 E2E 테스트를 대체하지 않는다.
