# MULTI Operation 추가 API

## API

```http
POST /api/v1/executions/{execution_id}/operations
```

`WAITING_FOR_OPERATION` 상태인 MULTI Execution에 다음 실행 계획을 추가한다. Agent가 이전
Operation의 결과를 확인하고 후속 코드 또는 오류 수정 코드를 제출할 때 사용한다.

## 호출 조건

- Execution의 `operation_mode`가 `MULTI`여야 한다.
- 현재 상태가 `WAITING_FOR_OPERATION`이어야 한다.
- `expected_version`이 현재 Execution `state.version`과 같아야 한다.
- 새 Step sequence는 기존 전체 Step 개수부터 연속·오름차순이어야 한다.

## Request Body

| 필드 | 필수 | 의미 |
|---|---:|---|
| `idempotency_key` | O | Operation 추가 명령의 멱등성 키 |
| `expected_version` | O | 상태조회 또는 이벤트에서 확인한 현재 Execution version. 0 이상 |
| `operation_timeout_seconds` | X | 새 Operation 전체 제한시간. 1초 이상 |
| `spec` | O | ExecutionSpec 1.0과 후속 Step 목록 |
| `metadata` | X | 새 Operation에 저장할 자유 형식 metadata. 기본값 `{}` |
| `actor.type` | O | 요청 주체: `AGENT`, `USER`, `BATCH` |
| `actor.id` | O | 요청 주체 식별자 |

### `spec`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `schema_version` | O | 현재 고정값 `1.0` |
| `steps` | O | 하나 이상의 후속 Step |

### `spec.steps[]`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `sequence` | O | Execution 전체 기준 sequence. 기존 Step 다음 번호부터 시작 |
| `payload.type` | O | 현재 `PYTHON_EXECUTE` |
| `payload.source` | O | INLINE 코드 또는 PATH Python 파일 |
| `step_timeout_seconds` | X | 해당 Step 실행 제한시간. 1초 이상 |
| `lineage` | X | Skill/Tool 및 입력 파라미터 추적 정보 |

INLINE source는 `{"type":"INLINE","content":"..."}` 형식이다. PATH source는
`{"type":"PATH","path":"...py","sha256":"64자리 checksum"}` 형식이며 입력 공유
루트 기준 상대경로, `.py`, UTF-8, checksum 일치를 요구한다.

`lineage`는 `skill_name`, `tool_name`, `input_parameters`를 지원한다. Operation/Execution
Step 최대 개수는 Executor 설정값을 넘을 수 없다.

## Request 예시

최초 Operation이 sequence `0`, `1`을 사용했다면 다음 Operation은 `2`부터 시작한다.

```json
{
  "idempotency_key": "task-100-operation-2",
  "expected_version": 4,
  "operation_timeout_seconds": 600,
  "spec": {
    "schema_version": "1.0",
    "steps": [
      {
        "sequence": 2,
        "payload": {
          "type": "PYTHON_EXECUTE",
          "source": {
            "type": "INLINE",
            "content": "display(result.describe())"
          }
        },
        "step_timeout_seconds": 300,
        "lineage": {
          "skill_name": "eda",
          "tool_name": "describe_data",
          "input_parameters": {}
        }
      }
    ]
  },
  "metadata": {"reason": "previous_result_followup"},
  "actor": {"type": "AGENT", "id": "analytics-agent"}
}
```

## Response: `202 Accepted`

| 필드 | 의미 |
|---|---|
| `execution_id` | 기존 MULTI Execution ID |
| `operation.operation_id` | 새로 생성된 Operation ID |
| `operation.steps[].sequence` | 접수된 후속 Step sequence |
| `operation.steps[].step_id` | 새로 발급된 논리 Step ID |
| `state.status` | 접수 후 `QUEUED` |
| `state.version` | Operation 추가로 증가한 최신 버전 |
| `created_by_type`, `created_by` | 최초 Execution 생성 주체 |
| `updated_by_type`, `updated_by` | Operation 추가 요청 주체 |
| `created_at`, `updated_at` | 생성 및 마지막 변경 시각 |

응답 `Location`은 새 Operation 상세조회 경로를 가리킨다. 실행 완료 후 다시
`WAITING_FOR_OPERATION`이 되면 Agent는 결과를 확인하여 또 Operation을 추가하거나 finalize한다.

동시에 같은 `expected_version`으로 두 요청이 들어오면 하나만 성공하고 나머지는 `409`가
된다. `409` 발생 시 최신 Execution 상태를 다시 조회해서 이미 처리된 명령인지 판단해야 한다.

