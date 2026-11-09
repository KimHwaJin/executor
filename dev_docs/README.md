# Executor 개발자 연동 문서

이 디렉토리는 Agent/BFF 연동에 필요한
문서 모음이며, 모든 운영 API에 개별 Markdown이 있는 것은 아니다.

## 먼저 볼 문서

- [API 레퍼런스](api-reference/README.md): 쿠베 배포 기준 API 44개(실행 25, 관리자 15, 헬스 3, MCP 1)와
  Redis 이벤트 8종의 종류별 명세. 코딩 에이전트에게는 영어판 [agent-guide.md](api-reference/agent-guide.md) 한 파일을 준다(한글판은 `agent-guide.ko.md`).
- [Executor API 사용 가이드](executor-api.md): 전체 API 목록, 헤더(`X-Api-Key`,
  `X-Tenant-Id`, `X-User-Id`, `X-Project-Id`, `X-Session-Id`)에 무엇을 넣는지,
  요청·응답 필드의 의미, 호출 순서와 예시. 다른 에이전트가 연동할 때 이 문서 하나로 시작한다.
- [Executor 이벤트](executor-events.md): Redis Stream으로 발행되는 이벤트 1.0의
  envelope, 8개 이벤트 유형, payload 필드, 순서·누락 복구 규칙.

식별자 규칙 요약: 신원과 범위(tenant, user, project, session)는 헤더, 대상 리소스는 path,
목록 필터는 query, 실행 내용은 body로 보낸다. body에 `actor`나 `context`는 보내지 않는다.

## 세부 문서

- [결과 인덱스](get-execution-result.md): `GET /api/v1/executions/{execution_id}/result`.
- [진단 이력](execution-diagnostics.md): `GET /api/v1/executions/{execution_id}/diagnostics`.
- [파일 다운로드](get-artifact-content.md): `GET /api/v1/artifacts/{artifact_id}/content`.
- [오류 응답](errors.md): 모든 API가 쓰는 `error.code`·`details` 형식, 409와 422의 구분,
  `details[].type`별 위치와 근거 값.
- [Agent 소비 가이드](agent-execution-event-consumer-guide.md): 순서·중복·누락 복구,
  SINGLE/MULTI wake-up 경계와 실패 처리.
- [Manifest 설명](step-result-manifest.md) / [JSON Schema](step-result-manifest.schema.json):
  코드·출력 원본을 읽는 파일 계약.
- [현재 시간 제한](execution-timeouts.md): 요청값의 단위, 범위, 대기·retry·만료 처리.

[Execution 전용 ReDoc HTML](execution-api-redoc.html)은 Execution 태그 경로와 component
schema 전체 필드를 담는다. 제출 API만 볼 때는
[`POST /api/v1/executions` ReDoc HTML](post-executions-redoc.html)을 사용한다.
Runtime 관리와 maintenance 운영 설명은 [전체 REST 가이드](../docs/rest-api.md)에 있다.
서비스의 `/docs`, `/redoc`, `/openapi`은 로컬 실행과 개발계에서 켜져 있고, 내부망 규정에 따라
stg와 prd에서는 꺼져 있다(`DOCS_ENABLED=false`, 404).

## 반드시 구분할 사항

1. 기본 커널 프로파일은 `default`, `3102311`이다. 배포 설정이 다르면 실제
   `RUNTIME_ALLOWED_PROFILES`와 서버의 지원 프로파일을 확인한다.
2. PATH 입력과 source/output/result ref는 모두 공통 루트 `STORAGE_ROOT` 기준이다.
   결과 ref는 `executor/executions/...` 형태이며 Agent도 같은 루트에 결합한다.
3. Jupyter의 notebook/report/artifact 파일은 Jupyter PV에 있다. Step 결과 원본은
   Agent/Executor 공유 PV에 있다. Artifact의 경로를 공통 루트에 결합하지 않는다.
4. `completed` 이벤트는 성공뿐 아니라 실패·취소도 나타낸다. status를 확인한다.
5. `CANCELLED`는 원격 커널 정리 성공을 보증하지 않는다. 상세의 recovery를 본다.
6. 노트북은 최초 저장 시부터 등록되고 실행 중·실패 후에도 다운로드할 수 있다.
   `AVAILABLE`은 분석 성공이나 모든 출력의 반영 완료를 뜻하지 않는다.
7. 전용 코드 계산시간·대기시간 누적 필드는 제공하지 않는다. Agent의 생존 신호를
   주기적으로 받거나 Agent 장애만을 이유로 실행을 자동 취소하지 않는다.

## 예시 사용 안내

예시의 사용자 ID, 실행 ID, 체크섬은 실제 요청 대상에 맞게 지정한다.
PATH 요청의 파일은 공통 루트 아래 자기 영역에 작성하고, 실행에 사용할 Runtime을 등록한다.
