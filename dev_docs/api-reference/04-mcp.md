# MCP 스펙

같은 서버의 `POST /mcp`가 MCP Streamable HTTP 엔드포인트다. 설정: `json_response=True`, `stateless_http=True`(세션 없음, 요청마다 독립). **dev 배포에서만 켜져 있고 stg·prd에서는 `/mcp`가 없다(404).** 운영 연동은 REST를 쓴다. 서버 이름 `executor-service`, **도구 28개**(실행 22 + Runtime Target 관리 6).

## 연결 규칙

- 인증·식별 헤더는 REST와 같다. MCP 연결의 HTTP 헤더로 `X-Api-Key`, `X-Tenant-Id`, `X-User-Id`, `X-Project-Id`, `X-Session-Id`를 보낸다. 같은 미들웨어가 REST와 MCP를 함께 검사한다.
- DNS rebinding 보호가 켜져 있다. `Host`는 `MCP_ALLOWED_HOSTS`, `Origin`은 `MCP_ALLOWED_ORIGINS`에 있어야 한다.
- 명령 도구는 `request` 하나를 받는다. REST 바디와 같은 필드에 경로 값(`execution_id`, `target_id`)이 더해진 객체다.
- 조회 도구는 경로·쿼리 값을 개별 인자로 받는다.
- 반환값은 REST 응답과 같은 모델이다(structured content). 노트북 셀 도구만 텍스트·이미지 콘텐츠 목록을 돌려준다.
- 오류는 MCP tool error이며 메시지는 `[CODE] message details=[...]` 형식이다. `CODE`는 REST의 `error.code`와 같다.
- `runtime_target_*` 도구는 admin key만 쓸 수 있다.

## REST에만 있는 기능

`GET .../steps/{step_id}`, `GET .../diagnostics`, `GET /artifacts/{id}/content`(다운로드), `DELETE /runtime-targets/{id}`, `GET /runtime-pools`, `/maintenance*` 6개, 헬스체크 3개.

## 도구 목록

| # | 도구 | 대응 REST | 반환 |
| ---: | --- | --- | --- |
| 1 | [`execution_submit`](#execution_submit) | `POST /api/v1/executions` | `ExecutionCommandResponse` |
| 2 | [`execution_get`](#execution_get) | `GET /api/v1/executions/{execution_id}` | `ExecutionResponse` |
| 3 | [`execution_cancel`](#execution_cancel) | `POST /api/v1/executions/{execution_id}/cancel` | `ExecutionCommandResponse` |
| 4 | [`execution_retry`](#execution_retry) | `POST /api/v1/executions/{execution_id}/retry` | `ExecutionCommandResponse` |
| 5 | [`execution_operation_create`](#execution_operation_create) | `POST /api/v1/executions/{execution_id}/operations` | `ExecutionCommandResponse` |
| 6 | [`execution_finalize`](#execution_finalize) | `POST /api/v1/executions/{execution_id}/finalize` | `ExecutionCommandResponse` |
| 7 | [`execution_notebook_read`](#execution_notebook_read) | `GET /api/v1/executions/{execution_id}/notebook` | `ExecutionNotebookResponse` |
| 8 | [`execution_notebook_cell_read`](#execution_notebook_cell_read) | `GET /api/v1/executions/{execution_id}/notebook/cells/{cell_index}` | `콘텐츠 목록` |
| 9 | [`execution_artifact_create`](#execution_artifact_create) | `POST /api/v1/executions/{execution_id}/artifacts` | `ExecutionArtifactResponse` |
| 10 | [`execution_result_get`](#execution_result_get) | `GET /api/v1/executions/{execution_id}/result` | `ExecutionResultResponse` |
| 11 | [`execution_operation_result_get`](#execution_operation_result_get) | `GET /api/v1/executions/{execution_id}/operations/{operation_id}/result` | `ExecutionOperationResultResponse` |
| 12 | [`execution_list`](#execution_list) | `GET /api/v1/executions` | `ExecutionPageResponse` |
| 13 | [`execution_step_list`](#execution_step_list) | `GET /api/v1/executions/{execution_id}/steps` | `ExecutionStepPageResponse` |
| 14 | [`execution_operation_list`](#execution_operation_list) | `GET /api/v1/executions/{execution_id}/operations` | `ExecutionOperationPageResponse` |
| 15 | [`execution_operation_get`](#execution_operation_get) | `GET /api/v1/executions/{execution_id}/operations/{operation_id}` | `ExecutionOperationResponse` |
| 16 | [`execution_operation_step_list`](#execution_operation_step_list) | `GET /api/v1/executions/{execution_id}/operations/{operation_id}/steps` | `ExecutionStepPageResponse` |
| 17 | [`execution_attempt_list`](#execution_attempt_list) | `GET /api/v1/executions/{execution_id}/attempts` | `ExecutionAttemptPageResponse` |
| 18 | [`execution_attempt_get`](#execution_attempt_get) | `GET /api/v1/executions/{execution_id}/attempts/{attempt_id}` | `ExecutionAttemptDetailResponse` |
| 19 | [`execution_attempt_step_list`](#execution_attempt_step_list) | `GET /api/v1/executions/{execution_id}/attempts/{attempt_id}/steps` | `ExecutionStepAttemptPageResponse` |
| 20 | [`execution_event_list`](#execution_event_list) | `GET /api/v1/executions/{execution_id}/events` | `ExecutionEventPageResponse` |
| 21 | [`execution_artifact_list`](#execution_artifact_list) | `GET /api/v1/executions/{execution_id}/artifacts` | `ExecutionArtifactPageResponse` |
| 22 | [`execution_artifact_get`](#execution_artifact_get) | `GET /api/v1/artifacts/{artifact_id}` | `ExecutionArtifactResponse` |
| 23 | [`runtime_target_upsert`](#runtime_target_upsert) | `POST /api/v1/runtime-targets` | `RuntimeTargetResponse` |
| 24 | [`runtime_target_list`](#runtime_target_list) | `GET /api/v1/runtime-targets` | `RuntimeTargetPageResponse` |
| 25 | [`runtime_target_get`](#runtime_target_get) | `GET /api/v1/runtime-targets/{target_id}` | `RuntimeTargetResponse` |
| 26 | [`runtime_target_probe`](#runtime_target_probe) | `POST /api/v1/runtime-targets/{target_id}/probe` | `RuntimeTargetResponse` |
| 27 | [`runtime_target_disable`](#runtime_target_disable) | `POST /api/v1/runtime-targets/{target_id}/disable` | `RuntimeTargetResponse` |
| 28 | [`runtime_target_set_state`](#runtime_target_set_state) | `POST /api/v1/runtime-targets/{target_id}/drain`<br>`POST /api/v1/runtime-targets/{target_id}/activate` | `RuntimeTargetResponse` |

<a id="execution_submit"></a>
### 1. `execution_submit`

> Persist an asynchronous execution request and return immediately with execution_id. Reusing idempotency_key with the same request returns the original execution.

대응 REST: `POST /api/v1/executions` — Execution 제출. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `ExecutionSubmitRequest` | O |  | 아래 필드를 담은 객체 |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.lifecycle` | ExecutionLifecycleInput | O |  | 실행 방식 |
| `request.trigger` | ExecutionTriggerInput | O |  | 트리거 |
| `request.runtime` | ExecutionRuntimeInput | O |  | Runtime 정보 |
| `request.operation` | ExecutionOperationInput | O |  | 첫 Operation |
| `request.metadata` | object |  |  | 호출자 소유 metadata(batch_id 등). 그대로 저장되고 상세 조회·`execution.completed`로 돌려받는다. 키 32개 이하, 키 `^[a-z][a-z0-9_]{0,63}$`, 직렬화 4096 bytes 이하. 비밀값 금지 |

`spec`·`operation`의 하위 필드는 [실행 API 스펙의 제출 바디](01-executions.md#ep-1)와 같다.

반환: `ExecutionCommandResponse` — 필드는 [응답 스키마](01-executions.md#schema-executioncommandresponse).

<a id="execution_get"></a>
### 2. `execution_get`

> Get the PostgreSQL-backed current execution state.

대응 REST: `GET /api/v1/executions/{execution_id}` — Execution 상태. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |

반환: `ExecutionResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionresponse).

<a id="execution_cancel"></a>
### 3. `execution_cancel`

> Request cancellation without waiting for worker acknowledgement. A successful call transitions a non-terminal execution to CANCEL_REQUESTED.

대응 REST: `POST /api/v1/executions/{execution_id}/cancel` — 취소 요청. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `ExecutionCancelRequest` | O |  | 아래 필드를 담은 객체 |
| `request.execution_id` | string(uuid) | O |  | Execution ID |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.reason` | string \| null |  | 길이≤2000 | 취소 사유(2000자 이하) |

반환: `ExecutionCommandResponse` — 필드는 [응답 스키마](01-executions.md#schema-executioncommandresponse).

<a id="execution_retry"></a>
### 4. `execution_retry`

> Resume a FAILED execution from its failed step using the retained Runtime session. The call queues a new attempt and returns immediately.

대응 REST: `POST /api/v1/executions/{execution_id}/retry` — 재시도. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `ExecutionRetryRequest` | O |  | 아래 필드를 담은 객체 |
| `request.execution_id` | string(uuid) | O |  | Execution ID |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |

반환: `ExecutionCommandResponse` — 필드는 [응답 스키마](01-executions.md#schema-executioncommandresponse).

<a id="execution_operation_create"></a>
### 5. `execution_operation_create`

> Append and queue one or more consecutive Steps as the next Operation for a waiting MULTI execution. expected_version prevents stale Agent decisions from being accepted.

대응 REST: `POST /api/v1/executions/{execution_id}/operations` — MULTI 다음 Operation 추가. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `ExecutionOperationCreateRequest` | O |  | 아래 필드를 담은 객체 |
| `request.execution_id` | string(uuid) | O |  | Execution ID |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.expected_version` | integer | O | ≥0 | 호출자가 알고 있는 현재 `state.version`. 다르면 `409` |
| `request.operation_timeout_seconds` | integer \| null |  | ≥1 | Operation이 RUNNING이 된 때부터 재는 시간 예산(초, 1 이상). 생략하면 제한 없음 |
| `request.spec` | ExecutionSpec | O |  | ExecutionSpec(실행할 Step 묶음) |
| `request.metadata` | object |  |  | 새 Operation의 metadata. 규칙은 제출 `metadata`와 같다 |

`spec`·`operation`의 하위 필드는 [실행 API 스펙의 제출 바디](01-executions.md#ep-1)와 같다.

반환: `ExecutionCommandResponse` — 필드는 [응답 스키마](01-executions.md#schema-executioncommandresponse).

<a id="execution_finalize"></a>
### 6. `execution_finalize`

> Finalize a waiting MULTI execution and release its retained Runtime session.

대응 REST: `POST /api/v1/executions/{execution_id}/finalize` — MULTI 종료. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `ExecutionFinalizeRequest` | O |  | 아래 필드를 담은 객체 |
| `request.execution_id` | string(uuid) | O |  | Execution ID |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.expected_version` | integer | O | ≥0 | 호출자가 알고 있는 현재 `state.version`. 다르면 `409` |

반환: `ExecutionCommandResponse` — 필드는 [응답 스키마](01-executions.md#schema-executioncommandresponse).

<a id="execution_notebook_read"></a>
### 7. `execution_notebook_read`

> Read the Runtime-owned Execution notebook. SUMMARY returns source previews and output summaries. FULL returns complete source and outputs for the requested page. start_index and limit provide bounded index pagination.

대응 REST: `GET /api/v1/executions/{execution_id}/notebook` — 노트북 페이지. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `view` | `SUMMARY` \| `FULL` |  | 기본 `"SUMMARY"` | `SUMMARY` 또는 `FULL` |
| `start_index` | integer |  | ≥0, 기본 `0` | 시작 셀 index |
| `limit` | integer |  | ≥1, ≤200, 기본 `20` | 페이지 크기 |

반환: `ExecutionNotebookResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionnotebookresponse).

<a id="execution_notebook_cell_read"></a>
### 8. `execution_notebook_cell_read`

> Read one Runtime-owned Notebook cell with complete source and all current outputs.

대응 REST: `GET /api/v1/executions/{execution_id}/notebook/cells/{cell_index}` — 노트북 셀 하나. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `cell_index` | integer | O | ≥0 | 셀 index(0부터) |

반환: 첫 항목은 셀 헤더 JSON(`index`, `id`, `type`, `execution_count`, `metadata`)과 소스 텍스트, 이후 출력마다 텍스트(stream·error·text/*)나 이미지(`image/png`, `image/jpeg`) 콘텐츠.

<a id="execution_artifact_create"></a>
### 9. `execution_artifact_create`

> Materialize Agent-authored UTF-8 text from INLINE content or an Executor input-PV PATH as a Runtime-owned Execution Artifact. DATASET and MODEL are Runtime/Manifest-produced and are not accepted by this command. REPORT files are written below reports/ and may also be appended to the notebook.

대응 REST: `POST /api/v1/executions/{execution_id}/artifacts` — 텍스트 Artifact 작성. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `ExecutionArtifactMaterializeToolRequest` | O |  | 아래 필드를 담은 객체 |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.type` | `NOTEBOOK` \| `REPORT` \| `PLOT` \| `METRIC` \| `LOG` \| `OTHER` | O |  |  |
| `request.source` | any | O |  | 코드 출처 |
| `request.name` | string \| null |  | 길이≥1, 길이≤255 | 이름 |
| `request.description` | string \| null |  | 길이≤4000 | 설명 |
| `request.media_type` | string \| null |  | 길이≥1, 길이≤255 | MIME 유형 |
| `request.append_to_notebook` | boolean |  | 기본 `false` | `true`면 노트북 끝에 Markdown 셀로도 붙인다. `REPORT`만 가능 |
| `request.metadata` | object |  |  |  |
| `request.execution_id` | string(uuid) | O |  | Execution ID |

반환: `ExecutionArtifactResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionartifactresponse).

<a id="execution_result_get"></a>
### 10. `execution_result_get`

> Get the compact authoritative Execution result after an Executor event signals availability. Includes current Operation and Step result references, Attempt summaries, and Artifact summaries.

대응 REST: `GET /api/v1/executions/{execution_id}/result` — 통합 결과. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |

반환: `ExecutionResultResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionresultresponse).

<a id="execution_operation_result_get"></a>
### 11. `execution_operation_result_get`

> Get one Operation together with all current Step results after an operation result event is received.

대응 REST: `GET /api/v1/executions/{execution_id}/operations/{operation_id}/result` — Operation 결과. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `operation_id` | string(uuid) | O |  | Operation ID |

반환: `ExecutionOperationResultResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionoperationresultresponse).

<a id="execution_list"></a>
### 12. `execution_list`

> List executions using an opaque cursor. Pass the returned next_cursor unchanged as cursor to continue. Optional filters remain fixed across pages.

대응 REST: `GET /api/v1/executions` — Execution 목록. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `project_id` | string \| null |  | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$` | 프로젝트로 거르기 |
| `session_id` | string \| null |  | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$` | 세션으로 거르기 |
| `trigger_type` | `INTERACTIVE` \| `BATCH` \| null |  |  | 트리거로 거르기 |
| `status` | `QUEUED` \| `RUNNING` \| `WAITING_FOR_OPERATION` \| `FINALIZING` \| `CANCEL_REQUESTED` \| `CANCELLED` \| `SUCCEEDED` \| `FAILED` \| null |  |  | 상태로 거르기 |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤200, 기본 `100` | 페이지 크기 |

반환: `ExecutionPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionpageresponse).

<a id="execution_step_list"></a>
### 13. `execution_step_list`

> List current execution Steps using an opaque MCP-style cursor. Pass an exact returned next_cursor as cursor to continue; never parse or modify it.

대응 REST: `GET /api/v1/executions/{execution_id}/steps` — Step 목록. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤200, 기본 `100` | 페이지 크기 |

반환: `ExecutionStepPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionsteppageresponse).

<a id="execution_operation_list"></a>
### 14. `execution_operation_list`

> List Agent-submitted execution Operations using an opaque cursor. An Operation is one accepted batch of consecutive Steps.

대응 REST: `GET /api/v1/executions/{execution_id}/operations` — Operation 목록. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤200, 기본 `100` | 페이지 크기 |

반환: `ExecutionOperationPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionoperationpageresponse).

<a id="execution_operation_get"></a>
### 15. `execution_operation_get`

> Get one accepted execution Operation detail.

대응 REST: `GET /api/v1/executions/{execution_id}/operations/{operation_id}` — Operation 상세. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `operation_id` | string(uuid) | O |  | Operation ID |

반환: `ExecutionOperationResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionoperationresponse).

<a id="execution_operation_step_list"></a>
### 16. `execution_operation_step_list`

> List current Step results belonging to one Operation.

대응 REST: `GET /api/v1/executions/{execution_id}/operations/{operation_id}/steps` — Operation의 Step 목록. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `operation_id` | string(uuid) | O |  | Operation ID |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤200, 기본 `100` | 페이지 크기 |

반환: `ExecutionStepPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionsteppageresponse).

<a id="execution_attempt_list"></a>
### 17. `execution_attempt_list`

> List immutable execution Attempt summaries with outcome and Step count.

대응 REST: `GET /api/v1/executions/{execution_id}/attempts` — Attempt 목록. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤200, 기본 `100` | 페이지 크기 |

반환: `ExecutionAttemptPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionattemptpageresponse).

<a id="execution_attempt_get"></a>
### 18. `execution_attempt_get`

> Get one immutable execution Attempt in detail.

대응 REST: `GET /api/v1/executions/{execution_id}/attempts/{attempt_id}` — Attempt 상세. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `attempt_id` | string(uuid) | O |  | Attempt ID |

반환: `ExecutionAttemptDetailResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionattemptdetailresponse).

<a id="execution_attempt_step_list"></a>
### 19. `execution_attempt_step_list`

> List immutable Step results for one Attempt using an opaque cursor.

대응 REST: `GET /api/v1/executions/{execution_id}/attempts/{attempt_id}/steps` — Attempt의 Step 결과. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `attempt_id` | string(uuid) | O |  | Attempt ID |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤200, 기본 `100` | 페이지 크기 |

반환: `ExecutionStepAttemptPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionstepattemptpageresponse).

<a id="execution_event_list"></a>
### 20. `execution_event_list`

> List the durable PostgreSQL event timeline and current Redis publication state for an execution.

대응 REST: `GET /api/v1/executions/{execution_id}/events` — 이벤트 이력. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `after_sequence` | integer |  | ≥0, 기본 `0` | 이 순번 다음 이벤트부터 |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤500, 기본 `200` | 페이지 크기 |

반환: `ExecutionEventPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executioneventpageresponse).

<a id="execution_artifact_list"></a>
### 21. `execution_artifact_list`

> List Artifacts produced by an execution with Attempt, Step, storage, checksum, and lineage references.

대응 REST: `GET /api/v1/executions/{execution_id}/artifacts` — Artifact 목록. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `execution_id` | string(uuid) | O |  | Execution ID |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤1000, 기본 `100` | 페이지 크기 |

반환: `ExecutionArtifactPageResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionartifactpageresponse).

<a id="execution_artifact_get"></a>
### 22. `execution_artifact_get`

> Get one Execution Artifact and its direct lineage references.

대응 REST: `GET /api/v1/artifacts/{artifact_id}` — Artifact 상세. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `artifact_id` | string(uuid) | O |  | Artifact ID |

반환: `ExecutionArtifactResponse` — 필드는 [응답 스키마](01-executions.md#schema-executionartifactresponse).

<a id="runtime_target_upsert"></a>
### 23. `runtime_target_upsert`

> Register or update a Runtime Target. The credential is encrypted at rest and never returned. The target is probed immediately and becomes schedulable only healthy.

대응 REST: `POST /api/v1/runtime-targets` — Target 등록·수정. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `RuntimeTargetUpsertRequest` | O |  | 아래 필드를 담은 객체 |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.name` | string | O | 길이≥1, 길이≤255, `^[a-zA-Z0-9._-]+$` | Target 이름 `^[a-zA-Z0-9._-]+$`. 같은 이름이면 수정 |
| `request.runtime_type` | `JUPYTER` | O |  | `JUPYTER` |
| `request.connection_config` | object | O |  | 접속 설정. 예: `{"endpoint": "http://jupyter-01:8888"}`. 같은 endpoint를 두 이름으로 등록하면 `409 RUNTIME_TARGET_ENDPOINT_CONFLICT` |
| `request.credential` | string(password) \| null |  |  | Jupyter 토큰. 암호화 저장되고 응답에 나오지 않는다 |
| `request.pool` | `INTERACTIVE` \| `BATCH` | O |  | `INTERACTIVE` 또는 `BATCH` |
| `request.max_concurrent_executions` | integer \| null |  | ≥1, ≤1000 | 동시 Execution 슬롯(1~1000). 생략하면 배포 기본값 |

반환: `RuntimeTargetResponse` — 필드는 [응답 스키마](02-admin.md#schema-runtimetargetresponse).

<a id="runtime_target_list"></a>
### 24. `runtime_target_list`

> List registered Runtime Targets and current capacity state.

대응 REST: `GET /api/v1/runtime-targets` — Target 목록. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `pool` | `INTERACTIVE` \| `BATCH` \| null |  |  | Pool로 거르기 |
| `runtime_type` | `JUPYTER` \| null |  |  | Runtime 종류로 거르기 |
| `status` | `ACTIVE` \| `DRAINING` \| `OFFLINE` \| null |  |  | 상태로 거르기 |
| `enabled` | boolean \| null |  |  | 사용 여부로 거르기 |
| `cursor` | string \| null |  |  | 이전 결과의 `next_cursor`를 그대로 |
| `limit` | integer |  | ≥1, ≤200, 기본 `100` | 페이지 크기 |

반환: `RuntimeTargetPageResponse` — 필드는 [응답 스키마](02-admin.md#schema-runtimetargetpageresponse).

<a id="runtime_target_get"></a>
### 25. `runtime_target_get`

> Get one registered Runtime Target without exposing its credential.

대응 REST: `GET /api/v1/runtime-targets/{target_id}` — Target 상세. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `target_id` | string(uuid) | O |  | Runtime Target ID |

반환: `RuntimeTargetResponse` — 필드는 [응답 스키마](02-admin.md#schema-runtimetargetresponse).

<a id="runtime_target_probe"></a>
### 26. `runtime_target_probe`

> Probe a Runtime Target now and persist its health and supported profiles.

대응 REST: `POST /api/v1/runtime-targets/{target_id}/probe` — 즉시 점검. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `RuntimeTargetProbeRequest` | O |  | 아래 필드를 담은 객체 |
| `request.target_id` | string(uuid) | O |  | Runtime Target ID |

반환: `RuntimeTargetResponse` — 필드는 [응답 스키마](02-admin.md#schema-runtimetargetresponse).

<a id="runtime_target_disable"></a>
### 27. `runtime_target_disable`

> Disable a Runtime Target. It remains queryable, is marked OFFLINE, and excluded from new scheduling.

대응 REST: `POST /api/v1/runtime-targets/{target_id}/disable` — 비활성화. 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `RuntimeTargetDisableRequest` | O |  | 아래 필드를 담은 객체 |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.target_id` | string(uuid) | O |  | Runtime Target ID |

반환: `RuntimeTargetResponse` — 필드는 [응답 스키마](02-admin.md#schema-runtimetargetresponse).

<a id="runtime_target_set_state"></a>
### 28. `runtime_target_set_state`

> Set a target to DRAINING to stop new scheduling while current work finishes, or probe and return it to ACTIVE.

대응 REST: `POST /api/v1/runtime-targets/{target_id}/drain`, `POST /api/v1/runtime-targets/{target_id}/activate` — 배정 중지(drain). 의미·주의는 REST 스펙을 따른다.

| 인자 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `request` | `RuntimeTargetSetStateRequest` | O |  | 아래 필드를 담은 객체 |
| `request.idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `request.target_id` | string(uuid) | O |  | Runtime Target ID |
| `request.desired_state` | string | O | `^(ACTIVE\|DRAINING)$` | `DRAINING`(배정 중지) 또는 `ACTIVE`(점검 후 재개) |

반환: `RuntimeTargetResponse` — 필드는 [응답 스키마](02-admin.md#schema-runtimetargetresponse).
