# Workflow Catalog Agent DB 기반 구성

## 기준과 구현 범위

`feature/workflow-recommendation`에서 `WorkflowCatalogService`를 추가했다.
API와 Agent가 같은 비동기 서비스 메서드를 직접 호출한다. HTTP 라우터,
요청·응답 변환과 인증·권한은 이번 변경에 포함하지 않는다. 등록·수정의 요청 키 기반 중복 방지는 공통 서비스에 구현하며 HTTP 헤더 전달·오류 매핑은 라우터에서 연결한다.
기존 파일 기반 `WorkflowService`는 사용하지 않는다.

초기 구현은 사용자 확정 정책과 당시 있던 updated.xlsx를 기준으로 진행했다.
이후 제공된 `docs/workflow_catalog_api/README.md`, `openapi.yaml` 및
`outputs/workflow_tables_20261012/workflow_tables_definition.xlsx`와 대조했다.
세 테이블의 컬럼·NULL·타입·키·제약조건은 일치하며 추가 DDL은 필요하지 않다.
서비스의 목록 페이지·필터·정렬, 검색 limit/threshold/execution_mode 및
search_text 반환, 추천 허용 반복 요청 무변경 규칙, 임베딩 오류·차원 검증을
OpenAPI 계약에 맞춰 보완했다. HTTP 응답 모델·라우터·오류 처리는 API 담당 범위다.

## DB와 모델 분리

- Agent DB: `WORKFLOW_DATABASE_URL`, 없으면 `EW_DATABASE_URL` 사용.
- Alembic: `migrations/`, `ew_alembic_version`, 현재 migration head `ew_0006`.
- chatapp: `crud_migrations/`에 별도의 `workflows`와 배열 기반
  `workflow_embeddings`가 있다. 이번 구현으로 변경하지 않는다.
- 모델: `app.models.workflow_catalog.AgentWorkflowBase`는 chatapp `Base`와
  별도 metadata를 사용한다. 일반 chatapp create_all에 등록하면 안 된다.
- Alembic은 기존대로 `EW_DATABASE_URL`을 읽는다. 별도 Workflow DB를
  사용하면 migration 실행 환경의 EW_DATABASE_URL을 해당 DB로 지정해야 한다.

카탈로그는 Workflow UUID당 한 행이다. 버전은 전체 Workflow 1.0 JSONB와
등록 주체 유형, 등록 사용자, 프로젝트·세션·Task 출처 UUID를 저장한다.
출처 UUID는 논리 참조이므로 chatapp FK가 없다. JSON actor는 작성 주체이며
등록 사용자나 registration_type과 같아야 한다는 제약을 두지 않는다.
컴파일된 1.3과 실행 입력값은 신규 reusable version에 저장하지 않는다.

현재 버전의 복합 FK는 DEFERRABLE INITIALLY DEFERRED로 catalog와 version을
한 트랜잭션에서 생성할 수 있다. 버전과 임베딩 FK는 물리 삭제를 차단한다.
추천·삭제 값 CHECK, revision 양수, 모델별 임베딩 UNIQUE,
차원 양수 및 vector_dims 일치 CHECK를 DB에서 검증한다.
VECTOR는 차원을 고정하지 않는다. 버전 불변성은 서비스에서 보장하며,
임의 DB UPDATE를 막는 트리거/권한 정책은 추가하지 않았다.

## 실행 이력과 기존 데이터 보존

현재 실행 저장은 workflow_executions가 선택한 workflow_version.version_id를 직접
참조한다. 실행 시작 당시의 컴파일 문서는 original_workflow, 확정된 입력값은
execution_inputs, 완료 문서는 final_workflow에 저장한다. 상태·시각·successful도
실행 기록에 저장한다. workflow_adaptive_history와 view는 실행을 계속 참조한다.

추천/생성 후보에 별도의 execution_plan_id를 발급하거나 실행계획 테이블에 쓰지 않는다.
신규 생성 Workflow 1.0은 선택 전에도 catalog/version/embedding에 등록하며,
실제 실행이 시작되기 전에는 workflow_executions 행을 생성하지 않는다.
선택된 version_id/revision을 실행 단계까지 유지하고 SQL FK·트리거 및 서비스 검증으로
ID/revision 일치를 확인한다. 같은 실행 ID를 다른 버전이나 Task로 재사용하면 거부한다.
재호출은 최초 original_workflow와 execution_inputs를 덮어쓰지 않고 완료는 최초 1회만 저장한다.

이관 단계:

- ew_0003: 기존 catalog를 legacy로 보존하고 신규 reusable 3개 테이블 생성.
- ew_0004: legacy 전체를 중간 실행계획으로 이관하며 알려진 버전만 연결.
  legacy/mapping 제거. 미확인 자료의 version_id는 NULL이며 임의 Workflow 1.0을 만들지 않는다.
- ew_0005: 실제 실행 기록의 버전·문서·입력·상태·시각을 유지한 채 중간
  workflow_execution_plans 테이블과 execution_plan_id 컬럼을 제거한다.

사용자 요청으로 ew_0005의 이관 보관 파일 생성 호출은 주석 처리했다.
파일 쓰기 권한이나 execution_plans_archive_dir 설정은 필요하지 않다.
중간 실행계획 테이블의 옛 후보 문서·legacy 메타데이터는 별도 보관 없이 제거한다.
실제 실행 기록의 original_workflow·execution_inputs·final_workflow·version_id와
adaptive 이력은 유지한다. 외부 FK/view가 남아 있으면 DB 변경 전체를 롤백한다.

기존 실행·adaptive 기록은 수정하거나 새 실행으로 꾸미지 않는다. version_id가 없는 옛
실행도 원본을 보존한다. 현재 운영 코드에는 legacy/mapping/중간 실행계획 SQL 및 ID가 없다.
과거 migration·이관 테스트의 참조는 DB 업그레이드 경로를 재현하기 위해 유지한다.

## 공통 요청 키 기반 중복 방지 (ew_0006)

보조 테이블 `workflow_catalog_requests`를 추가한다. 도메인의 catalog/version/embedding 세 테이블은 유지한다.
`requested_by` UUID와 `request_key` TEXT가 복합 PK이며 `request_hash`, 최초 결과 JSONB `result`, `created_at`을 저장한다.
사용자 UUID는 논리 참조로 FK를 설정하지 않는다. 요청 기록은 자동 만료하지 않는다. 삭제/다운그레이드로 요청 기록을
없애면 해당 키의 중복 방지 보장도 사라지므로 임의 정리는 하지 않는다.

- `register`/`update`의 선택 인자 `idempotency_key`는 앞뒤 공백 없는 1~255자 문자열이다.
- API는 POST /workflows와 PUT /workflows/{workflow_id}의 Idempotency-Key 헤더를 해당 인자로 전달한다.
  사용자 범위는 요청 created_by/updated_by이며 등록·수정은 같은 키 공간이다. 키는 인증 증빙이 아니다.
- 에이전트는 `agent:workflow-register:{task_id}:{workflow_revision}:{candidate_index}`를 전달한다.
  같은 generated draft 등록 노드를 재시도하면 동일 키를 사용하고, 새 생성 회차/후보 순번은 새 키를 사용한다.
  이 키는 등록 재시도를 보호하며 LLM 생성 호출 자체의 재실행을 막지는 않는다.
- 정규화된 Workflow 문서, 작업, 대상 workflow_id, expected_revision, registration_type, 사용자와 출처 UUID를
  정렬된 JSON으로 해시한다. 객체 키 순서는 무시하며 배열 순서와 실제 본문 변경은 구분한다.
- 같은 사용자/키/본문은 최초 성공 결과를 그대로 반환한다. 이후 recommend_yn 변경·수정·논리 삭제가 있어도
  최신 상태를 재조회하지 않는다. UUID/datetime 타입을 복원하고 기존 라우터 응답 변환을 그대로 적용한다.
- 같은 키에 다른 본문·작업·대상을 전달하면 `WorkflowIdempotencyConflictError` (409 IDEMPOTENCY_KEY_CONFLICT)다.
- 저장된 성공 결과를 먼저 확인하고 DB 연결을 반환한 뒤 임베딩을 호출한다. 같은 키 동시 요청의 임베딩 호출은
  겹칠 수 있지만 저장 트랜잭션의 advisory lock과 재확인으로 catalog/version/embedding은 한 번만 저장한다.
- 성공 결과 저장도 도메인 데이터와 같은 트랜잭션이다. 오류·롤백은 키를 소비하지 않는다.
- 동일 키의 성공한 수정 재요청은 expected_revision 검사보다 먼저 최초 결과를 반환한다.
  키를 생략하거나 새 키로 오래된 expected_revision을 요청하면 기존 버전 충돌 409다.
- 키 없는 호출은 기존 동작을 유지한다. PATCH recommendation / DELETE는 기존 반복 요청 규칙을 유지한다.

## 서비스 호출 계약

```python
from app.services.workflow_catalog_service import WorkflowCatalogService, MockEmbeddingProvider

# 기본 연결: API_BASE_URL / MODEL_API_KEY / EMBEDDING_MODEL_NAME 사용.
service = WorkflowCatalogService.from_environment()
# 모델 서버 없는 로컬 테스트에서만 명시적으로 mock 주입:
service = WorkflowCatalogService.from_environment(MockEmbeddingProvider())
item = await service.register(
    workflow=document, registration_type="USER", created_by=user_uuid,
    idempotency_key="registration-request-uuid",
    project_id=project_uuid, session_id=None, task_id=None,
)
updated = await service.update(
    item["workflow_id"], expected_revision=1, workflow=new_document,
    registration_type="AGENT", updated_by=user_uuid,
    idempotency_key="update-request-uuid",
)
await service.set_recommendation(item["workflow_id"], recommend_yn="Y", updated_by=user_uuid)
detail = await service.get(item["workflow_id"])  # revision=1로 이력 조회 가능
items = await service.list(page=1, page_size=20, recommend_yn="Y", sort_by="updated_at", sort_order="desc")
matches = await service.search("사용자 검색 문장", limit=3, threshold=None, execution_mode=None)
await service.delete(item["workflow_id"], deleted_by=user_uuid)
```

반환값은 DB UUID/datetime을 포함하는 dict이며 HTTP 직렬화는 라우터에서 처리한다.
`WorkflowNotFoundError`는 404, `WorkflowRevisionConflictError`는 409 WORKFLOW_REVISION_CONFLICT,
`WorkflowIdempotencyConflictError`는 409 IDEMPOTENCY_KEY_CONFLICT에 연결한다.
`WorkflowVersionNotFoundError`는 404 WORKFLOW_VERSION_NOT_FOUND,
`WorkflowValidationError`는 422 WORKFLOW_VALIDATION_ERROR,
`WorkflowEmbeddingTimeoutError`는 504 EMBEDDING_TIMEOUT,
`WorkflowEmbeddingError`는 502 EMBEDDING_SERVICE_ERROR에 연결한다.
Timeout 예외는 EmbeddingError의 하위 클래스이므로 먼저 처리한다.
UUID·필터·범위 검증 ValueError는 422 REQUEST_VALIDATION_ERROR로 처리하며,
DB 예외는 500 INTERNAL_ERROR로 처리한다. 내부 cause/SQL/접속 정보는 출력하지 않는다.
삭제는 None을 반환하고 반복 삭제도 성공한다. 미존재 항목은 404 예외다.
삭제 항목은 조회·목록·수정·추천 허용 변경·검색에서 제외한다.

라우터의 응답 변환은 다음과 같다. datetime은 UTC ISO 8601, UUID는 문자열로
변환하고 additionalProperties=false 스키마에서 허용하는 필드만 반환한다.

| 메서드 | OpenAPI 응답 / 변환 |
|---|---|
| register/update | 반환 dict에서 workflow_id/version_id/revision/registration_type/recommend_yn 선택 → WriteResponse |
| set_recommendation | workflow_id/current_revision/recommend_yn 선택 → RecommendationResponse |
| get | catalog 메타데이터는 최상위, version_id/revision/workflow/registration_type/project_id/session_id/task_id/search_text는 version 내부에 배치. version_created_by → version.created_by, version_created_at → version.created_at |
| list | items/page/page_size/total_count envelope. items는 본문·벡터·search_text 없는 ListItem |
| search | items/returned_count/limit/threshold/embedding_model envelope. items는 Workflow 전체와 search_text를 포함하고 벡터 제외 |
| delete | 본문 없는 204 |

목록 q는 앞뒤 공백 제거 후 name/user_request의 대소문자 무시 부분 문자열을
검색하며 %, _도 문자 그대로 처리한다. 추천 여부·실행 모드·등록 유형·최초
catalog 등록자 필터는 AND로 적용한다. 기본 page=1/page_size=20,
sort_by=updated_at/sort_order=desc이고 동점은 workflow_id 오름차순이다.
건수와 페이지는 같은 DB 스냅샷에서 조회한다. 페이지 초과도 실제 total_count를 반환한다.
목록은 임베딩을 호출하지 않는다.

등록/수정은 StandardWorkflow와 기존 compile_workflow로 구조·Skill/Tool·인자·
출력 연결·조건을 검증한다. 컴파일 결과는 검증에만 사용한다.
요청 키의 성공 결과를 사전 조회하는 경우 DB 연결을 반환한 뒤 임베딩을 호출한다.
임베딩 호출 중 저장 트랜잭션·잠금을 유지하지 않는다. 저장 시 catalog 행 잠금으로
expected_revision 비교, 신규 버전·임베딩 INSERT, current_revision 변경을
같은 트랜잭션에서 처리한다. 충돌로 버려진 임베딩 결과는 DB에 적재하지 않는다.
수정 시 catalog.created_by와 recommend_yn은 유지하며 새 버전.created_by에는
updated_by를 기록한다. 제공하지 않은 새 버전의 출처는 NULL이다.

검색 텍스트 recipe 1은 name/user_request/execution_mode와 Skill 설명·Tool
docstring을 결합한다. `OpenAICompatibleEmbeddingProvider`가 기존 연결 설정을
사용해 실제 임베딩 API를 호출한다. `model` 식별자와
`async embed(text) -> list[float]` 인터페이스를 제공한다. Mock은 재현 가능한 1024차원
벡터를 만들며 식별자 `mock/bge-m3/1024/v1`로 실제 임베딩과 구분한다.
Mock 점수는 의미 유사도 품질 검증에 사용할 수 없다.
동일 모델·동일 차원, 미삭제·추천 Y·현재 버전만 정확 cosine 검색한다.
검색 기본 limit=3, 범위 1..20, threshold=None 또는 -1..1이다.
execution_mode는 None/static/adaptive다. 검색 질문은 앞뒤 공백을 제거한다.
임계값 필터 후 점수 내림차순·workflow_id 오름차순으로 limit을 적용한다.
서비스는 provider 반환 벡터를 검증하며 기본 기대 차원은 1024다.
`from_environment()`는 EMBEDDING_DIMENSION을 읽는다. 다른 모델은 이 값을
실제 출력 차원으로 변경하거나 constructor의 embedding_dimension을 지정한다.
상세 search_text는 선택 모델의 조회 버전 임베딩이 없으면 None이다.
추천 허용을 같은 값으로 요청하면 updated_at/updated_by까지 그대로 유지한다.
근사 인덱스는 추가하지 않았다. pgvector 동작은
[공식 문서](https://github.com/pgvector/pgvector)를 따른다.

## 적용 및 검증

로컬 Agent DB에는 ew_0006을 적용하여 workflow_catalog_requests 생성을 확인했다.
기존 catalog/version/embedding과 실행·adaptive 이력은 변경하지 않는다.
다른 환경에는 해당 Agent DB 설정으로 `uv run --env-file .env alembic upgrade ew_0006`을 적용한 뒤 새 코드를 실행한다.

### 에이전트 연결 및 유사도 기반 생성 전환

실제 모델 의존성 생성 시 `WorkflowCatalogService`를 생성해 검색 adapter와
생성 후보 저장 노드에 같은 인스턴스를 주입한다. API 호출 없이 서비스 메서드를
직접 사용한다. `WORKFLOW_RECOMMENDATION_ENABLED=true`에서 활성화되고,
현재 서버 설정의 `WORKFLOW_SIMILARITY_SCORE` 기본값은 0.8이다.
모델·임베딩 오류는 빈 후보로 바꾸지 않고 요청 실패로 전달한다.

그래프는 announce_workflow_search → recommend_workflow 순서로 진행한다.
사용자 질문으로 현재 추천 가능한 버전을 설정된 개수만큼 검색하고, 제외된 구조를
건너뛴 뒤 유사도 순으로 최대 3개(기본 설정)를 제시한다. API와 에이전트 모두 >=를 적용하며,
사용자의 에이전트 전환 정책은 **기준 이상(>=) 추천 / 기준 미만(<) 또는 후보 없음이면 생성**이다.
임계값은 유한한 -1..1 값으로 검증한다. 기본값 기준으로 0.8도 추천에 포함한다.
`WORKFLOW_RECOMMENDATION_LIMIT`으로 추천 후보 수를 설정한다(기본 3, 허용 1..20).
`WORKFLOW_GENERATION_LIMIT`로 생성·재생성 회차별 후보 수를 설정한다(기본 2, 허용 1..20).
이는 목표 개수다. 추가 후보가 3회 재시도 후에도 중복이면 이미 검증된 후보만 등록·표시하고
요청 개수와 실제 개수를 안내한다. 유효한 후보가 하나도 없거나 스키마·컴파일 검증이 실패한 경우에는
오류를 유지하며, 중복 후보를 개수 충족용으로 저장하지 않는다.
각 후보는 서로 다른 실행 구조로 생성·검증한 뒤 개별 카탈로그/버전/임베딩으로 등록한다.
재생성은 기존 생성 후보 모두를 제외하고 새 후보 묶음으로 교체한다.
`workflow_revision`과 `MAX_WORKFLOW_REVISIONS`는 후보 개수가 아니라 생성 회차를 센다.
추천 결과는 `recommendations` 배열이며 `recommendation`은 호환용 첫 번째 후보다.
`recommend_again`은 기존 후보를 유지하고 새 후보를 최대 `WORKFLOW_RECOMMENDATION_LIMIT`개 추가한다.
기존 workflow_id는 DB 검색 LIMIT 전에 제외해 다음 후보를 가져온다. 누적 후보 수는 3개를 넘을 수 있다.
추가 후보가 없어도 기존 후보 선택 화면을 유지한다. 최초 검색에 후보가 없을 때만 생성으로 넘어간다.
프론트 후보 목록에는 누적 후보 전체와 각 후보의 workflow_id/version_id/revision/similarity_score를 전달한다.
표시 개수는 프론트가 정할 수 있으며 기존 candidate_id·버전·실행 이력 참조를 유지한다.

- 기준 이상 후보 존재: 생성 에이전트를 호출하지 않고 기존 후보 선택 HITL로 진행.
- 후보 없음 또는 기준 미만: generate_workflow로 진행하고 생성 원본을 신규 서비스에 등록.
- 추천 후보 화면에서 기존 generate 행동 선택: 추천을 한 번 건너뛰고 생성으로 진행.
- 추천 비활성화: 검색을 호출하지 않고 기존 생성 흐름 사용.

새 생성 원본은 registration_type=AGENT, created_by=요청 user_id로 등록한다.
project_id/session_id/task_id를 버전 생성 출처로 전달하고 execution_inputs는
버전 JSON에 넣지 않는다. 등록 workflow_id/version_id/revision을 후보와 상태에
기록한다. 최초 recommend_yn=N이므로 추천 후보로 공개하려면 별도 허용 변경이 필요하다.

추천은 저장된 1.0 원본을 유지하며 선택 후 실행 입력을 별도로 수집하고 1.3으로
컴파일한다. HITL의 workflow_revision은 로컬 계획 차수이고,
workflow_content_revision/version_id는 DB에서 선택한 내용 버전이다.
실행 이력에는 실제 내용 revision을 기록한다.
실제 실행 시작 시 workflow_executions에 version_id와 컴파일 문서·실행 입력을
직접 저장한다. 후보 제시에 따른 별도의 실행계획 저장은 없다.

모델 없는 단위 테스트는 catalog·LLM 의존성을 주입한다. 기존 MOCK_LLM 모드는
네트워크 없는 고정 생성 fixture를 유지하므로 실제 catalog 연결 테스트에는
catalog 서비스를 별도로 주입한다. 실제 모델·DB 없이도 경계 점수, 생성 버튼,
추천 선택 뒤 입력 HITL과 버전 전달을 검증한다. 실 DB 테스트는 별도 격리 DB와
1024차원 mock provider로 생성 → 등록 → 추천 → 실행 이력 매핑을 검증했다.
추천 분기 테스트 23개가 통과했다. 기준 미만·경계값 0.8 포함·후보 없음·높은 점수,
추천 비활성화, 생성 버튼의 반복 추천 방지, 입력 HITL로의 연결,
검색 실패 전달, 잘못된 임계값·후보 수 설정, 최대 후보 수 변경 및
두 번째·세 번째 후보 선택 시 해당 버전 유지를 검증했다.

### 고객사 임베딩 API 연결

임베딩 연결은 LLM의 MODEL_PROVIDER와 독립적으로 설정한다. 따라서
MODEL_PROVIDER=azure_openai에서도 OpenAI 호환 임베딩 서버를 사용할 수 있다.
EMBEDDING_API_BASE_URL을 우선 사용하고, 비어 있으면 API_BASE_URL을 재사용한다.
인증에는 MODEL_API_KEY를 사용하며 AZURE_OPENAI_API_KEY와는 별개다.
LLM_MODEL_NAME은 LLM 이름으로 유지하고 EMBEDDING_MODEL_NAME과 EMBEDDING_DIMENSION을
별도로 설정한다. 차원 값은 양의 정수여야 한다.
`agent_config.py`의 `AgentSettings` / `load_agent_settings()`가 환경변수 또는
기존 `.env` 파일에서 설정을 읽는다. `config.py`에는 임베딩 설정을 두지 않는다.
`WorkflowCatalogService.from_environment()`는 기본적으로 실제 HTTP provider를
생성하며, 로컬 mock은 명시적으로 주입할 때만 사용한다.

```dotenv
EMBEDDING_MODEL_NAME=bge-m3
EMBEDDING_DIMENSION=1024
```

provider는 API_BASE_URL 뒤에 `/embeddings`를 붙여 POST하고, 기존 키를 Bearer
Authorization으로 전달한다. MODEL_TIMEOUT_SECONDS를 같은 방식으로 사용한다.
[OpenAI 호환 요청 형식](https://developers.openai.com/api/reference/resources/embeddings/methods/create)을 가정한다.

```json
{"model":"bge-m3","input":"검색용 텍스트 또는 사용자 질문","encoding_format":"float"}
```

`data[0].embedding`의 숫자 배열을 받으며 서비스에서 설정 차원·유한 숫자·
nonzero vector를 검증한다. 모델명 필드가 반환되면 요청 모델과 같은지도 확인한다.
dimensions 요청 옵션으로 차원을 강제하지 않는다. bge-m3 서버의 실제 1024차원
반환을 검사하는 방식이다. 설정 차원은 모델의 반환 차원을 바꾸는 요청이 아니라
실제 응답에 대한 검증·검색 기준이다. 모델 또는 출력 차원을 바꾸면 재임베딩이
필요하다. HTTP/파싱/벡터 오류는 502, timeout은 504이며
오류가 나도 mock으로 대체하지 않는다. API 키·내부 오류 본문은 응답에 포함하지 않는다.

고객사 내부망에 실제 접속한 테스트는 아직 하지 않았다. 로컬에서는 HTTP
MockTransport로 위 요청 처리 코드를 실행하고 1024차원 배열 응답을 반환하여
provider 및 DB 저장·검색을 검증한다. 실제 API가 이 계약과 다른 경우에는
해당 서버 규격에 맞춰 provider 요청/응답 부분을 조정해야 한다.

실제 임베딩 연결로 바꿔도 추가 DB migration은 필요 없다. 기존 mock 벡터의
모델 식별자는 `mock/bge-m3/1024/v1`이므로 실제 `bge-m3` 검색에 포함되지 않는다.
mock으로 만든 버전을 실제 검색에 사용할 때는 해당 버전을 재임베딩해야 한다.

최초 구현 검증 때는 실제 DB에 migration을 적용하지 않았다.
초기 sandbox 안의 연결 점검은 실패했으나, 권한을 확보한 읽기 전용 점검으로
설정된 로컬 Agent DB에 연결했다. 기존 catalog 7행, execution 1행,
adaptive history 0행을 확인했다. catalog 문서는 Workflow 1.0 5행과 compiled
1.3 2행이며 workflow_id/revision 중복 그룹이 1건 있다.
실제 execution.catalog_id FK는 workflow_catalog.catalog_id를 참조한다.
그 점검 당시 새 workflow_version/workflow_embeddings와 vector 확장은 없었다.
이후 사용자가 로컬 DB에서 ew_0003 적용 완료를 알려주었다. 문서 대조 후
읽기 전용으로 재확인한 상태는 ew_0003, legacy catalog/mapping 각 8행,
execution 1행, 새 세 테이블 각 0행, vector 0.8.7 활성화다.
이후 로컬 DB에 ew_0004, ew_0005를 순서대로 적용했다. 옛 후보 9행은 1회 이관
보관 파일로 대조·보존했고, 기존 실행 1행의 버전·문서·입력·상태·시각과 adaptive
이력이 변하지 않았음을 확인했다. legacy/mapping과 중간 실행계획 테이블 및 ID 컬럼은
제거했다. 고객사 DB에는 적용하지 않았다.
PostgreSQL 16.15는 설치되어 있었으며 pgvector 0.8.7을 공식 소스로 설치했다.
검증용 클러스터는 기존 데이터 디렉터리·환경 파일과 분리하고 Unix 소켓만 열었다.

환경변수는 Secret 등 기존 방식으로 주입하고 값을 출력하지 않는다.

```bash
python scripts/check_workflow_catalog_db.py
alembic current
alembic upgrade head
```

데이터가 있는 기존 catalog에서는 기본 upgrade가 변경 전에 실패한다.
사용자는 로컬·고객사 모두 별도 백업을 생략하는 정책을 확인했다.
대상 DB·보존 전환·배포 코드 검토를 마친 경우 다음 보존 옵션을 사용한다.

```bash
alembic -x preserve_legacy_catalog=true upgrade head
```

동일 이름의 embeddings가 이미 있으면 Agent/chatapp DB 오접속 또는 충돌을
확인하도록 실패한다. ew_0003 downgrade는 새 catalog가 비어 있을 때만 가능하다.
ew_0004/ew_0005 자동 downgrade는 지원하지 않으며 별도 복원 migration이 필요하다. 실행 데이터를 자동 삭제하지 않는다.

ew_0003 DB의 적용 순서: API/Worker를 중지 → 대상 DB 및 참조 확인 →
`uv run --env-file .env alembic upgrade ew_0006` → 개편 코드로 API/Worker 재시작.
별도 WORKFLOW_DATABASE_URL 사용 시 Alembic의 EW_DATABASE_URL도 같은 DB를 지정한다.
읽기 전용 사전 확인 연결은 upgrade 전에 종료해야 한다.

테스트는 `WORKFLOW_CATALOG_TEST_ADMIN_URL`에 명시한 로컬 서버에 테스트별
고유 DB를 생성하여 실행한다. admin DB 자체에는 migration을 적용하지 않는다.
테스트 종료 시 생성한 DB만 삭제한다. 서버에 CREATE DATABASE 권한과
pgvector extension 설치가 필요하다.

```bash
python -m pytest src/app/test/test_workflow_catalog_db.py -q
```

실제 DB 검증 57개 통과: 빈 DB upgrade, populated DB 적용 차단·명시 보존,
중복된 legacy 식별자·실행 FK·adaptive view 보존, downgrade 보호,
모든 legacy 메타데이터 보존·이관, 외부 FK/잘못된 매핑의 전체 롤백,
실행 원본·입력 불변성, 완료 중복 호출 보호, 중간 계획 제거 시 실행 문서·입력·버전 유지,
1024차원 mock 저장, 현재 추천 버전/모델/차원 필터,
동시 수정 충돌, 임베딩·DB 실패 롤백, DB CHECK/FK 제약, 중간 계획 테이블·컬럼 제거와 버전 직접 참조 저장.
추가로 보관 파일 미생성·파일 쓰기 경로 불필요, 외부 FK가 남은 경우 전체 DB 롤백,
확인된 버전 참조와 실제 실행 문서·입력 보존을 검증한다.
추가로 모델/DDL 컬럼·NULL 일치, ORM VECTOR 저장·조회, revision/모델 UNIQUE,
기존 동명 embeddings 테이블이 있는 DB에서 migration 차단을 검증했다.
제공된 OpenAPI 등록 예시와 Write/Recommendation/List/Detail/Search 응답의
실제 JSON Schema를 검증했다. 목록 필터·문자 그대로의 q·정렬·페이지 초과
건수, 검색 limit/threshold/실행 모드·동점 정렬, 과거 버전 search_text 부재,
버전 없음 오류, 추천 허용 반복 요청의 감사 정보 유지, 임베딩 오류·시간 초과·
잘못된 차원/NaN/zero vector 실패, 임베딩 대기 중 추천 변경·삭제 경쟁도 검증했다.
추가 HTTP provider 테스트 28개 통과: 기존 접속 설정·별도 임베딩 모델명·
dotenv 로딩, 실제 POST 요청 파싱·1024 배열, HTTP 오류·timeout·잘못된 응답
차단 및 DB 호출 없음. DB 통합에서는 HTTP mock 응답으로 register/update/search를
실행하고 bge-m3/1024 벡터 저장을 확인했다.
환경변수·dotenv 차원 변경, 4096차원 반환 수용, 설정 차원 불일치 거부,
0/음수/문자열/소수 차원 설정 거부도 검증했다.

기존 컴파일러·저장 테스트는 22개 통과 / 5개 실패했다. 이번 작업에서 변경하지
않은 adaptive fixture에는 현재 eda_analysis 규칙의 필수 histogram_eda가 없고,
lot_cd nullable 가정도 현재 Registry의 str 타입과 불일치한다.

규격 디렉터리의 static 예시는 현재 Registry의 필수 `query_mode`와 맞지 않는다.
규격 문서는 읽었으며, 테스트 입력은 현재 Registry와 일치하는 기존
`src/app/test/fixtures/workflow_standard/static.json`을 사용했다.

추가 검증: 같은 키 등록/수정 재호출·최초 결과 보존·본문/작업/출처 충돌·사용자 범위 분리,
동시 등록/수정 단일 저장, 트랜잭션 롤백 후 재시도, 여러 후보 등록 중 실패 후 에이전트 재호출을 검사한다.
