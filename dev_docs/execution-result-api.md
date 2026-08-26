# Execution Result API 가이드

## 1. 개요

```http
GET /api/v1/executions/{execution_id}/result
```

Execution의 현재 상태와 Operation, Step, Attempt, Artifact 결과를 한 번에 조회하는
Agent용 통합 결과 인덱스 API다.

이 API는 코드, 텍스트, 이미지 같은 실행 결과 원본을 응답에 직접 포함하지 않는다.

- PostgreSQL에서 Execution, Operation, Step, Attempt, Artifact 정보를 조회한다.
- Step 출력은 `output_summary`로 요약한다.
- 실제 코드와 출력은 `result_ref`가 가리키는 공유 PV의 manifest 및 출력 파일에서 읽는다.
- 응답 배열은 페이지네이션 없이 해당 Execution의 전체 이력을 반환한다.

```json
{
  "execution": {},
  "operations": [],
  "attempts": [],
  "artifacts": []
}
```

## 2. `execution`

Execution 전체의 현재 상태다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `execution.execution_id` | UUID | Executor가 발급한 Execution 식별자 |
| `execution.state.status` | enum | Execution의 현재 상태 |
| `execution.state.version` | integer | 상태 갱신 버전. 상태 변경 시 증가하며 이전 조회 결과와 최신 상태를 구분할 때 사용 |

### Execution 상태

| 값 | 의미 |
|---|---|
| `QUEUED` | 실행 대기 중 |
| `DISPATCHED` | Worker가 작업을 할당받음 |
| `RUNNING` | Runtime에서 실행 중 |
| `WAITING_FOR_OPERATION` | MULTI 모드에서 다음 Operation 입력 대기 중 |
| `FINALIZING` | 최종 종료 처리 중 |
| `CANCEL_REQUESTED` | 취소 요청을 받았지만 Runtime 정리가 끝나지 않음 |
| `CANCELLED` | 취소 및 Runtime 정리 완료 |
| `SUCCEEDED` | 전체 실행 성공 |
| `FAILED` | 전체 실행 실패 |

## 3. `operations[]`

Execution에 제출된 Operation 목록이다.

- SINGLE 모드에서는 일반적으로 Operation이 한 개다.
- MULTI 모드에서는 최초 제출과 이후 추가한 Operation들이 모두 포함된다.
- `operation_number` 오름차순으로 반환된다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `operation_id` | UUID | Executor가 발급한 Operation 식별자 |
| `operation_number` | integer | Execution 내 Operation 순번. `1`부터 시작 |
| `sequence_range.first` | integer | 이 Operation에 포함된 첫 Step sequence |
| `sequence_range.last` | integer | 이 Operation에 포함된 마지막 Step sequence |
| `result.status` | enum | Operation 실행 상태 |
| `result.error_message` | string/null | Operation 실패 또는 취소 사유. 정상 실행이면 `null` |
| `lifecycle.started_at` | datetime/null | Operation 실행 시작 시각 |
| `lifecycle.finished_at` | datetime/null | Operation 종료 시각 |
| `steps` | array | 이 Operation에 포함된 현재 논리 Step 목록 |

`sequence_range.first`와 `sequence_range.last`는 모두 범위에 포함된다.

### Operation 상태

| 값 | 의미 |
|---|---|
| `QUEUED` | 실행 대기 중 |
| `RUNNING` | 실행 중 |
| `SUCCEEDED` | Operation 전체 성공 |
| `FAILED` | Operation 실패 |
| `CANCELLED` | Operation 취소 |

## 4. `operations[].steps[]`

각 Operation에 속한 논리 Step과 현재 결과다. `sequence` 순으로 반환된다.

이 목록은 현재 논리 Step 결과이며, 재시도별 StepAttempt 전체 이력은 아니다.

### Step 식별 및 순서

| 필드 | 타입 | 의미 |
|---|---|---|
| `step_id` | UUID | Executor가 발급한 논리 Step 식별자 |
| `sequence` | integer | Execution 전체에서 Step 실행 순서. 일반적으로 `0`부터 시작 |

### `lineage`

Agent가 제출한 Skill 및 Tool 추적 정보다. 실제 Python 코드에서 역으로 추출하지 않고
Step metadata로 받은 값을 저장한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `lineage.skill_name` | string/null | 해당 Step이 속한 Skill 이름 |
| `lineage.tool_name` | string/null | 실행하려던 Tool 이름 |
| `lineage.input_parameters` | object | Agent가 제출한 Tool 입력 파라미터 및 추적 metadata |

### `result`

| 필드 | 타입 | 의미 |
|---|---|---|
| `result.status` | enum | Step의 현재 실행 상태 |
| `result.output_summary` | object | 출력 종류와 크기에 대한 요약 |
| `result.error_message` | string/null | Step 실패 메시지 |
| `result.result_ref` | object/null | 공유 PV에 저장된 실제 Step 결과 manifest 참조 |

### Step 상태

| 값 | 의미 |
|---|---|
| `PENDING` | 아직 실행되지 않음 |
| `RUNNING` | 실행 중 |
| `SUCCEEDED` | 성공 |
| `FAILED` | 실패 |
| `SKIPPED` | 이전 Step 실패 등의 이유로 실행하지 않음 |
| `CANCELLED` | 취소됨 |

### `result.output_summary`

실제 출력 원본을 열지 않고 출력 특성을 판단하기 위한 요약이다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `output_count` | integer | Jupyter 출력 레코드 개수. 텍스트 줄 수가 아님 |
| `output_types` | object | 출력 종류별 개수 |
| `stream_names` | string[] | 발견된 Stream 이름 목록 |
| `mime_types` | string[] | 출력에 포함된 MIME 타입 목록 |
| `has_image` | boolean | 이미지 MIME 출력이 하나 이상 있는지 |
| `image_count` | integer | 이미지 representation 개수 |
| `has_error` | boolean | Jupyter `error` 출력이 포함됐는지 |

`output_types` 예시는 다음과 같다.

```json
{
  "stream": 2,
  "display_data": 1,
  "execute_result": 1,
  "error": 0
}
```

주요 출력 종류는 다음과 같다.

| 종류 | 의미 |
|---|---|
| `stream` | `stdout` 또는 `stderr` 출력 |
| `display_data` | `display()` 또는 그래프 출력 |
| `execute_result` | 셀 마지막 표현식 결과 |
| `error` | Jupyter 실행 오류 |

`mime_types`에는 `text/plain`, `text/html`, `application/json`, `image/png` 등이
포함될 수 있다.

### `result.result_ref`

실제 코드와 출력 원본을 찾기 위한 공유 PV 참조다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `storage` | `SHARED_PV` | 결과가 Agent와 Executor의 공유 PV에 저장됐다는 의미 |
| `execution_id` | UUID | 결과가 속한 Execution |
| `step_id` | UUID | 결과가 속한 Step |
| `attempt_id` | UUID | 이 결과를 실제 생성한 Execution Attempt |
| `fencing_token` | integer | 결과를 기록한 Worker lease 세대 번호. 오래된 Worker 결과를 구분하는 데 사용 |
| `relative_path` | string | 공유 PV 루트 기준 Step 결과 `manifest.json` 경로 |
| `checksum_sha256` | string | `manifest.json` 파일의 SHA-256 checksum |
| `complete` | boolean | manifest 기록이 정상적으로 종료됐는지 |
| `representation_count` | integer | 출력에 저장된 MIME representation 총개수 |
| `total_size_bytes` | integer | 출력 representation 파일 크기의 합계 |

`relative_path`는 절대경로가 아니다. Agent는 자신의 공유 PV 마운트 루트에
`relative_path`를 결합해야 한다.

```text
Agent의 SHARED_STORAGE_ROOT
└── result_ref.relative_path
```

예를 들어 다음과 같은 응답을 받았다고 가정한다.

```json
{
  "storage": "SHARED_PV",
  "relative_path": "executions/abc/results/attempt-1/step-1/manifest.json"
}
```

Agent의 공유 PV 마운트 루트가 `/workspace/shared`라면 실제 파일은 다음과 같다.

```text
/workspace/shared/executions/abc/results/attempt-1/step-1/manifest.json
```

manifest에는 다음 정보가 포함된다.

- 실행한 코드 source 참조
- 출력 레코드 목록
- Stream, DisplayData, ExecuteResult, Error 구분
- 텍스트, JSON, HTML, 이미지 등의 representation 파일 경로
- Jupyter execution count
- 실행 오류 정보

`representation_count`는 `output_count`와 다르다. 하나의 `display_data`가
`text/plain`과 `image/png` 두 가지 표현을 가지면 `output_count`는 1이고
`representation_count`는 2다.

`total_size_bytes`는 출력 representation 파일 크기의 합이며 코드 파일이나 manifest
자체 크기는 포함하지 않는다.

### `lifecycle`

| 필드 | 타입 | 의미 |
|---|---|---|
| `lifecycle.started_at` | datetime/null | Step 실행 시작 시각 |
| `lifecycle.finished_at` | datetime/null | Step 성공, 실패 또는 취소 종료 시각 |

아직 시작하지 않았거나 종료되지 않은 경우 해당 값은 `null`이다.

## 5. `attempts[]`

Execution의 실행 시도 이력이다. 최초 실행과 재시도가 각각 별도 Attempt로 저장되며
`attempt_number` 오름차순으로 반환된다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `attempt_id` | UUID | Execution Attempt 식별자 |
| `execution_id` | UUID | 소속 Execution 식별자 |
| `attempt_number` | integer | 실행 시도 순번. `1`부터 시작 |
| `state.status` | enum | Attempt 상태 |
| `failure` | object/null | Attempt 실패 정보 |
| `lifecycle.started_at` | datetime/null | Attempt 시작 시각 |
| `lifecycle.finished_at` | datetime/null | Attempt 종료 시각 |
| `step_count` | integer | 해당 Attempt에 속한 StepAttempt 개수 |

### Attempt 상태

| 값 | 의미 |
|---|---|
| `RUNNING` | 실행 중 |
| `WAITING` | MULTI 모드에서 다음 Operation 대기 중 |
| `SUCCEEDED` | 성공 |
| `FAILED` | 실패 |
| `CANCELLED` | 취소 |

### `attempts[].failure`

| 필드 | 타입 | 의미 |
|---|---|---|
| `failure.type` | enum | 구조화된 실패 분류 |
| `failure.message` | string | 실패 상세 메시지 |

정상 실행이거나 실패 정보가 아직 확정되지 않았다면 `failure`는 `null`이다.

| 실패 유형 | 의미 |
|---|---|
| `TOOL_ERROR` | 실행한 코드 또는 Tool 오류 |
| `INFRASTRUCTURE_ERROR` | 인프라 오류 |
| `WORKER_SHUTDOWN` | Worker 종료 |
| `RUNTIME_UNAVAILABLE` | 사용 가능한 Runtime 없음 |
| `LEASE_EXPIRED` | 실행 lease 만료 |
| `INTERNAL_ERROR` | Executor 내부 오류 |
| `OPERATION_WAIT_TIMEOUT` | 다음 Operation 대기시간 초과 |
| `OPERATION_TIMEOUT` | Operation 전체 실행시간 초과 |
| `STEP_TIMEOUT` | 개별 Step 실행시간 초과 |
| `EXECUTION_TIMEOUT` | Execution 최대 실행시간 초과 |
| `OUTPUT_LIMIT_EXCEEDED` | 허용 출력 크기 초과 |
| `RUNTIME_SESSION_LOST` | Runtime 세션 유실 |

## 6. `artifacts[]`

Execution에서 등록된 Artifact 요약 목록이다. 생성시각과 `artifact_id` 순으로 반환된다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `artifact_id` | UUID | Artifact 식별자 |
| `name` | string | Artifact 이름 |
| `type` | enum | Artifact 종류 |
| `status` | enum | Artifact 상태 |
| `produced_by` | object | 어떤 Execution, Attempt, Step이 생성했는지 나타내는 정보 |
| `storage` | object | 저장소 및 파일 요약 |

### Artifact 종류

- `DATASET`
- `NOTEBOOK`
- `REPORT`
- `PLOT`
- `MODEL`
- `METRIC`
- `LOG`
- `OTHER`

### Artifact 상태

| 값 | 의미 |
|---|---|
| `AVAILABLE` | 정상 사용 가능 |
| `INCOMPLETE` | 생성 또는 등록이 불완전 |
| `DELETED` | Soft delete됨 |

### `artifacts[].produced_by`

| 필드 | 타입 | 의미 |
|---|---|---|
| `execution_id` | UUID | Artifact를 생성한 Execution |
| `execution_attempt_id` | UUID/null | Artifact를 생성한 Execution Attempt |
| `execution_step_id` | UUID/null | Artifact를 생성한 논리 Step |
| `execution_step_attempt_id` | UUID/null | Artifact를 실제 생성한 StepAttempt |

Execution 전체에서 생성한 Artifact라면 Step 관련 ID는 `null`일 수 있다.

### `artifacts[].storage`

| 필드 | 타입 | 의미 |
|---|---|---|
| `type` | `PV`/`S3` | Artifact 저장소 유형 |
| `media_type` | string/null | MIME 타입. 예: `image/png`, `text/markdown` |
| `size_bytes` | integer/null | 파일 크기 |

이 API는 Artifact 요약만 제공하므로 URI와 파일 경로는 포함하지 않는다. 실제 위치와
다운로드 정보는 다음 API를 사용한다.

```http
GET /api/v1/artifacts/{artifact_id}
GET /api/v1/artifacts/{artifact_id}/content
```

## 7. 공통 감사 필드

`attempts[]`와 `artifacts[]` 각각에는 다음 감사 필드가 포함된다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `created_by_type` | enum/null | 생성 주체 유형: `AGENT`, `USER`, `BATCH` |
| `created_by` | string/null | 생성 주체 ID |
| `updated_by_type` | enum/null | 마지막 수정 주체 유형 |
| `updated_by` | string/null | 마지막 수정 주체 ID |
| `created_at` | datetime | 생성 시각 |
| `updated_at` | datetime | 마지막 수정 시각 |

현재 Result API의 `execution`, `operations`, `steps`에는 응답 축약을 위해 감사 필드가
포함되지 않는다.

## 8. 반환 순서와 데이터 원본

| 항목 | 반환 순서 | 주 데이터 원본 |
|---|---|---|
| `operations` | `operation_number` 오름차순 | PostgreSQL |
| `operations[].steps` | `sequence` 오름차순 | PostgreSQL |
| `attempts` | `attempt_number` 오름차순 | PostgreSQL |
| `artifacts` | `created_at`, `artifact_id` 오름차순 | PostgreSQL |
| 실제 코드와 출력 | `result_ref.relative_path` 참조 | Agent/Executor 공유 PV |

Result API 호출 자체는 공유 PV의 manifest나 출력 파일을 읽지 않는다. PostgreSQL에
저장된 요약과 파일 참조를 반환하며, 실제 원본이 필요한 Agent가 공유 PV에서 직접 읽는다.

## 9. Agent 권장 사용 흐름

1. Redis에서 Operation 또는 Execution terminal 이벤트를 받는다.
2. `GET /api/v1/executions/{execution_id}/result`를 호출한다.
3. `execution.state.status`로 전체 상태를 확인한다.
4. `operations[].steps[].result.output_summary`로 출력 종류와 오류 여부를 판단한다.
5. 실제 코드, 텍스트 또는 이미지가 필요하면 `result_ref.relative_path`의 manifest를
   공유 PV에서 읽는다.
6. manifest의 source 및 output representation 경로를 이용해 필요한 파일만 읽는다.
7. Notebook, Report, Plot 등의 등록된 산출물은 `artifacts[]`에서 식별하고 Artifact
   상세조회 또는 다운로드 API를 사용한다.

이 API는 결과 원본 반환 API가 아니라, Execution 전체 상태와 결과 원본 위치를 한 번에
찾기 위한 통합 결과 인덱스 API다.
