# Workflow Catalog API

규격 버전: **1.1.0** · 작성일: **2026-10-12** · 갱신일: **2026-10-13**

신규 `WorkflowCatalogService`의 API 계약서입니다. DB 모델·마이그레이션(`ew_0006`)과 공통 서비스는 구현되어 있으며, API와 에이전트가 같은 비동기 서비스 메서드를 직접 호출합니다. HTTP 라우터·요청/응답 모델·응답 변환·오류 매핑은 API 담당자가 연결할 범위입니다. 기존 파일 저장 기반 `WorkflowService`는 사용하지 않습니다.

## 전달 파일

| 파일 | 내용 |
|---|---|
| [openapi.yaml](openapi.yaml) | API 경로, 요청·응답 스키마, 오류, 요청·응답 예시, 상세 처리 규칙 |
| README.md | 구현 범위와 공통 정책 안내 |
| [workflow_catalog_db.md](../workflow_catalog_db.md) | 현재 구현 상태, 서비스 호출 계약, 응답 변환·오류 매핑, DB 마이그레이션 안내 |

OpenAPI YAML에 Workflow 1.0 스키마와 예시를 모두 포함했습니다. 별도 예시 JSON 없이 OpenAPI 3.1을 지원하는 Swagger UI 또는 ReDoc에서 확인할 수 있습니다. 예시 UUID·시각·유사도·검색용 텍스트는 설명용입니다.

## API 목록

경로는 공통 API prefix를 제외한 값입니다. 실제 서버 주소와 prefix는 연결 시 결정합니다.

| 메서드 | 경로 | 기능 |
|---|---|---|
| POST | `/workflows` | 최초 등록 |
| POST | `/workflows/search` | 추천용 유사 검색 |
| GET | `/workflows` | 현재 버전 기준 관리용 목록 |
| GET | `/workflows/{workflow_id}` | 현재 또는 지정 버전 상세 |
| PUT | `/workflows/{workflow_id}` | JSON 전체 수정 및 새 버전 생성 |
| PATCH | `/workflows/{workflow_id}/recommendation` | 추천 허용 변경 |
| DELETE | `/workflows/{workflow_id}` | 논리 삭제 |

목록·상세 요청은 Query 파라미터를 사용합니다. 삭제는 `deleted_by`를 필수 Query로 전달하며, 성공 응답은 본문 없는 `204`입니다. 각 파라미터의 필수 여부·기본값·범위는 YAML을 기준으로 합니다.

## Workflow 문서와 등록 정보

등록·수정 요청의 `workflow`에는 **Workflow 1.0 문서 전체**를 넣습니다.

```text
요청
├─ workflow
│  ├─ workflow_version: "1.0"
│  └─ workflow
│     ├─ actor
│     ├─ name
│     └─ ...
├─ registration_type
└─ created_by 또는 updated_by
```

| 필드 | 의미 |
|---|---|
| `workflow.workflow.actor` | JSON을 작성한 주체 |
| `registration_type` | 해당 버전의 등록 주체 유형. `USER` 또는 `AGENT` |
| `created_by` / `updated_by` | 등록·수정을 요청한 사용자 UUID |
| `project_id`, `session_id`, `task_id` | 해당 버전의 생성 출처. 생략·null이면 새 버전에 NULL 저장 |

Workflow 구조는 저장소의 `workflow_schema_1.0.json`을 포함했습니다. Skill·Tool 소속, 필수 인자·타입, 출력 참조 및 조건은 별도 의미 검증 대상입니다. 현재 인자 source는 `literal`, `input`, `tool_output`이며, `input`의 실행값은 등록 시 요구하지 않습니다. 예시는 현재 Registry의 `extract_data`, `transform_data`를 사용합니다.

## 저장·수정·삭제 정책

| 테이블 | 역할 |
|---|---|
| `workflow_catalog` | 워크플로우당 한 행. 현재 버전·추천 허용·논리 삭제 관리 |
| `workflow_version` | 버전별 불변 Workflow 1.0 JSON과 등록·출처 정보 |
| `workflow_embeddings` | 버전별 검색 텍스트·모델·벡터·차원 수 |

- 최초 등록: `revision=1`, `current_revision=1`, `recommend_yn=N`.
- 내용 수정: `workflow_catalog.workflow_id`를 유지하고, `workflow_version`에 `revision=expected_revision+1`인 새 행을 생성합니다. 새 `version_id`에 연결된 `workflow_embeddings` 행에 `search_text`, `embedding_model`, `embedding`, `embedding_dimension`을 저장하고, `workflow_catalog.current_revision`을 새 `revision`으로 갱신합니다. **기존 `workflow_catalog.recommend_yn` 값은 유지합니다.**
- 수정 요청의 `updated_by`는 새 버전의 `created_by`에도 저장합니다. 카탈로그 최초 등록자·시각은 유지합니다.
- 요청의 `expected_revision`과 `workflow_catalog.current_revision` 비교, 새 버전·임베딩 저장, `current_revision` 갱신은 같은 트랜잭션에서 처리합니다. 불일치는 `409 WORKFLOW_REVISION_CONFLICT`이며 DB 변경 없이 종료합니다. 수정 성공 시에도 기존 `workflow_catalog.recommend_yn` 값은 유지합니다.
- 목록·검색은 현재 버전만 반환합니다. 상세는 `revision` Query로 과거 버전을 조회할 수 있습니다.
- `recommend_yn` 변경은 `workflow_catalog.recommend_yn`과 `updated_by`, `updated_at`만 갱신합니다. `current_revision`은 유지하고 `workflow_version`·`workflow_embeddings`에 새 행을 생성하지 않습니다. 이미 같은 `recommend_yn` 값이면 `updated_by`, `updated_at`도 변경 없이 `200`입니다.
- 최초·반복 삭제 모두 `204`, 존재하지 않는 ID는 `404`입니다. 반복 삭제 시 최초 삭제자·시각을 유지합니다.
- 논리 삭제 후 목록·상세·검색에서 제외하고, 버전·임베딩·실행 이력은 보존합니다.
- 사용자·프로젝트·세션·Task UUID는 `chatapp` DB 논리 참조이며, 다른 DB를 대상으로 FK를 설정하지 않습니다.

## 검색 정책

- 초기 모델은 `bge-m3`, 출력은 **1,024차원**입니다. 저장 타입은 `VECTOR`, `embedding_dimension`은 실제 벡터 길이와 일치해야 합니다.
- 검색 대상은 다음 조건을 모두 충족하는 행입니다. 프로젝트는 생성 출처이며 검색 범위 제한에 사용하지 않습니다.
  - `workflow_catalog.deleted_at IS NULL`
  - `workflow_catalog.recommend_yn = 'Y'`
  - `workflow_version.workflow_id = workflow_catalog.workflow_id` 및 `workflow_version.revision = workflow_catalog.current_revision`
  - `workflow_embeddings.version_id = workflow_version.version_id`
  - `workflow_embeddings.embedding_model`이 현재 임베딩 제공자의 모델 식별자와 일치
  - `workflow_embeddings.embedding_dimension`이 검색 질의의 임베딩 차원과 일치(서비스에서 `EMBEDDING_DIMENSION`과도 검증)
- `similarity = 1 - cosine_distance`이며 확률이 아닙니다.
- `limit` 기본값은 `3`, 범위는 `1~20`입니다.
- `threshold`는 기본 `null`로 점수 하한이 없습니다. 지정 범위는 `-1~1`입니다.
- 임계값 이상인 결과를 유사도 내림차순으로 반환하고, 동점은 `workflow_id` 오름차순입니다. 결과가 부족해도 임계값을 낮추지 않습니다.
- 검색 결과가 없으면 `200`과 `items: []`를 반환합니다. 임베딩·DB 오류를 빈 결과로 대체하지 않습니다.
- 검색 결과에는 `search_text`와 Workflow 문서 전체를 포함하며, 벡터는 반환하지 않습니다.
- 목록의 `q`는 임베딩을 호출하지 않는 문자열 검색입니다. 상세의 `search_text`는 해당 모델의 텍스트가 없으면 `null`입니다.

현재 검색용 텍스트는 Workflow의 name/user_request/execution_mode와 Skill 설명·Tool docstring을 결합하며, `workflow_embeddings.text_recipe_version='1'`로 저장합니다. YAML의 임계값 `0.7`은 사용 예시입니다. 에이전트는 `WORKFLOW_SIMILARITY_SCORE`(기본 `0.8`)를 사용하며, search API의 threshold 기본값은 `null`입니다.

## 등록·수정 재요청 중복 방지

등록·수정 API는 선택 헤더 `Idempotency-Key`를 서비스의 `idempotency_key` 인자로 전달합니다.
키는 앞뒤 공백 없는 1~255자 문자열이며 요청 사용자 UUID(`created_by`/`updated_by`)별로 등록·수정이 같은 키 공간을 공유합니다.
같은 키와 같은 정규화된 본문이면 최초 성공 결과를 반환하며 새 catalog/version/embedding을 생성하지 않습니다.
같은 키에 다른 본문·작업·workflow_id·expected_revision·출처를 전달하면 `409 IDEMPOTENCY_KEY_CONFLICT`입니다.
키 없는 요청은 기존 처리이며 재요청 중복 방지를 보장하지 않습니다. 키는 인증·권한 검사를 대체하지 않습니다.

`workflow_catalog_requests`에 요청 해시와 최초 서비스 결과를 본문 저장과 같은 트랜잭션으로 보관합니다.
성공 기록은 자동 만료하지 않으며 같은 키를 새 요청에 재사용하지 않습니다. 실패·롤백은 키를 소비하지 않습니다.
동시 요청은 키 단위 DB 잠금 후 재확인하여 한 번만 저장합니다. 동시 최초 요청은 임베딩 호출이 겹칠 수 있지만 DB에는 한 번만 저장됩니다.
이미 성공한 재요청은 임베딩을 다시 호출하지 않습니다. 최초 결과는 이후 수정·추천 변경·논리 삭제가 있어도 그대로 재전달됩니다.
API는 등록 `201`/수정 `200`과 기존 WriteResponse 변환을 그대로 사용하며 별도 응답 필드를 추가하지 않습니다.
에이전트는 `agent:workflow-register:{task_id}:{생성 회차}:{후보 순번}` 키를 전달합니다.

**수정 충돌 방지용 `expected_revision`과 `409 WORKFLOW_REVISION_CONFLICT`는 유지**합니다.
같은 요청 키·본문으로 성공한 수정을 재요청하면 버전 검사 전에 최초 성공 결과를 반환합니다.
키가 없거나 새로운 키로 이전 expected_revision을 요청하면 기존처럼 버전 충돌 `409`입니다.
recommend_yn 변경과 논리 삭제의 반복 요청 처리 규칙도 그대로 유지합니다.

## 공통 오류

공통 오류 형식:

```json
{
  "error": {
    "code": "WORKFLOW_REVISION_CONFLICT",
    "message": "워크플로우가 수정되었습니다. 최신 버전을 조회한 뒤 다시 요청해주세요.",
    "details": [
      {
        "field": "expected_revision",
        "reason": "현재 버전과 일치하지 않습니다.",
        "expected_revision": 1,
        "current_revision": 2
      }
    ]
  }
}
```

| HTTP 상태 | 의미 |
|---|---|
| 422 | 요청·Workflow 검증 실패 |
| 404 | 대상 또는 버전 없음 |
| 409 | 수정 요청의 버전 충돌 또는 Idempotency-Key 재사용 충돌 |
| 502 | 임베딩 서비스·반환 벡터 오류 |
| 504 | 임베딩 시간 초과 |
| 500 | DB 등 내부 오류 |

이번 범위에서는 인증·권한 검사와 `401`·`403`을 제외합니다. 요청의 사용자 UUID는 인증 증빙이 아닙니다. 내부 예외·SQL·비밀정보는 오류 응답에 노출하지 않습니다.

## 구현 연결 사항

API 라우터와 에이전트는 `WorkflowCatalogService`의 공통 메서드를 사용합니다. 에이전트가 HTTP API를 호출할 필요는 없습니다.

| 구성 | 담당 범위 |
|---|---|
| 라우터 | 요청 모델·서비스 연결·공통 응답 및 오류 매핑 |
| 서비스 | 구조·의미 검증, 텍스트 구성, 모델 호출, DB 트랜잭션, 수정 버전 충돌 검사 |

- 임베딩 호출은 DB 트랜잭션 전에 수행하고, 저장 직전에 삭제·버전 변경 여부를 다시 확인합니다. 추천 변경과의 동시 처리도 기존 값을 잃지 않도록 해야 합니다.
- 등록·수정의 Idempotency-Key 헤더 전달 및 `WorkflowIdempotencyConflictError` → `409 IDEMPOTENCY_KEY_CONFLICT` 매핑을 연결합니다.
- 세 도메인 테이블의 마이그레이션 담당은 구현 착수 시 협의합니다.
- 기존 `/workflows` API가 있으므로 실제 prefix와 라우터 교체 시점을 협의합니다. `/workflows/search`와 동적 경로의 라우팅 충돌도 피해야 합니다.
- 생성 전환, 사용자 거절, 즉시 실행 조건은 에이전트 시나리오 정책으로 별도 정의합니다.

## 검증 범위

YAML 구문, OpenAPI 구조 모델, 내부 참조, 인라인 예시의 JSON 스키마·UUID·시각을 검증했습니다. 잘못된 인자 범위·등록 유형 거부와 Workflow 예시의 Tool 소속·필수 인자도 확인했습니다. 실제 HTTP 서버·DB·임베딩 호출 및 실행 통합 테스트는 구현 이후 진행합니다.
