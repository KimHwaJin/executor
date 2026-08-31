# Step Result Manifest 1.0

## 목적

`GET /api/v1/executions/{execution_id}/result`의
`operations[].steps[].result.result_ref.relative_path`가 가리키는 terminal
`manifest.json` 규격이다.

기계 판독용 JSON Schema는
[step-result-manifest.schema.json](step-result-manifest.schema.json)에 있다.

manifest는 Agent/Executor 공유 PV에 존재한다. Agent는 자신의 공유 PV 마운트 루트에
`result_ref.relative_path`를 결합해 파일을 읽는다.

Redis Step 완료 이벤트와 Operation 완료 이벤트의 `step_results[].result_ref`도
같은 manifest를 가리킨다. 참조의 `complete`와 manifest의 `complete`를 대조한다.
`false`인 봉인된 manifest도 부분 증거로 읽을 수 있으며 Result API를 추가 호출할
필요는 없다. 개별 표현 파일의 checksum/크기는 별도로 검증한다.

## 전체 예시

```json
{
  "schema_version": "1.0",
  "state": "FINALIZED",
  "complete": true,
  "identity": {
    "execution_id": "10000000-0000-0000-0000-000000000001",
    "operation_id": "20000000-0000-0000-0000-000000000002",
    "step_id": "30000000-0000-0000-0000-000000000003",
    "sequence": 0,
    "execution_attempt_id": "40000000-0000-0000-0000-000000000004",
    "fencing_token": 1
  },
  "source": {
    "relative_path": "executions/10000000-0000-0000-0000-000000000001/sources/30000000-0000-0000-0000-000000000003/source.py",
    "checksum_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "size_bytes": 14
  },
  "outputs": [
    {
      "ordinal": 0,
      "kind": "STREAM",
      "stream_name": "stdout",
      "execution_count": null,
      "representations": [
        {
          "media_type": "text/plain",
          "encoding": "UTF8",
          "relative_path": "outputs/000000-stream-00.txt",
          "size_bytes": 6,
          "checksum_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "complete": true,
          "truncated_in_preview": false,
          "metadata": {}
        }
      ],
      "metadata": {},
      "created_at": "2026-08-26T03:00:00+00:00"
    }
  ],
  "output_count": 1,
  "representation_count": 1,
  "total_size_bytes": 6,
  "execution_count": 1,
  "error_message": null,
  "output_summary": {
    "output_count": 1,
    "output_types": {"stream": 1},
    "stream_names": ["stdout"],
    "mime_types": ["text/plain"],
    "has_image": false,
    "image_count": 0,
    "has_error": false
  },
  "created_at": "2026-08-26T03:00:00+00:00",
  "updated_at": "2026-08-26T03:00:01+00:00",
  "completed_at": "2026-08-26T03:00:01+00:00"
}
```

예시의 checksum은 구조 설명을 위한 자리표시자다. 실제 파일에서는 반드시 실제 SHA-256과
일치해야 한다.

## 최상위 필드

| 필드 | 의미 |
|---|---|
| `schema_version` | Manifest 계약 버전. 현재 `1.0` |
| `state` | 결과 봉인 상태: `FINALIZED`, `FAILED`, `ABORTED` |
| `complete` | Runtime 출력 스트림을 완전하게 수집했는지 |
| `identity` | 이 결과의 Execution/Operation/Step/Attempt/fence 식별정보 |
| `source` | 실제 실행 코드 snapshot 참조 |
| `outputs` | Runtime이 보낸 출력 레코드 목록 |
| `output_count` | `outputs` 원소 개수 |
| `representation_count` | 모든 output에 포함된 representation 개수 합계 |
| `total_size_bytes` | 모든 representation 파일 크기 합계 |
| `execution_count` | Jupyter cell execution count 또는 `null` |
| `error_message` | 실패·중단 메시지 또는 `null` |
| `output_summary` | Result API에도 저장되는 제한된 출력 요약 |
| `created_at` | 결과 수집 시작 시각 |
| `updated_at` | 마지막 갱신 시각 |
| `completed_at` | terminal manifest 봉인 시각 |

### `state`와 `complete`

| `state` | `complete` | 의미 |
|---|---:|---|
| `FINALIZED` | `true` | Step이 정상 종료되고 출력 수집도 완료 |
| `FAILED` | `true` | Step 실행 오류가 정상적으로 수신돼 오류 출력까지 완료 |
| `ABORTED` | `false` | timeout, 취소, output message 제한 등으로 출력 스트림이 중간 종료 |

`complete=true`는 Step 성공을 뜻하지 않는다. `FAILED`도 오류 결과를 끝까지 수집했다면
`complete=true`다. 성공 여부는 `state` 또는 Result API의 Step status로 판단한다.

실행 중에는 `<fencing-token>.partial/.state.json`이 사용되지만 이는 공개 계약이나 권위 있는
결과가 아니다. Agent는 `result_ref`로 반환된 terminal `manifest.json`만 읽어야 한다.

## `identity`

| 필드 | 의미 |
|---|---|
| `execution_id` | 소속 Execution ID |
| `operation_id` | 소속 Operation ID |
| `step_id` | 소속 논리 Step ID |
| `sequence` | Execution 전체 기준 Step sequence |
| `execution_attempt_id` | 실제 결과를 만든 Execution Attempt ID |
| `fencing_token` | 결과를 만든 Worker lease 세대 번호 |

Agent는 `execution_id`, `step_id`, `execution_attempt_id`, `fencing_token`이 Result API의
`result_ref`와 일치하는지 확인해야 한다. 더 오래된 fencing generation 디렉터리가 남아
있어도 DB가 선택한 `result_ref`만 권위 있는 결과다.

## `source`

| 필드 | 의미 |
|---|---|
| `relative_path` | 공유 PV 루트 기준 실행 코드 snapshot 경로 |
| `checksum_sha256` | source 파일 SHA-256 |
| `size_bytes` | source 파일 byte 크기 |

`source.relative_path`는 manifest 디렉터리가 아니라 Agent 공유 PV 루트를 기준으로 해석한다.
읽은 파일의 크기와 checksum을 모두 확인해야 한다.

## `outputs[]`

| 필드 | 의미 |
|---|---|
| `ordinal` | Step 내 출력 순번. `0`부터 증가 |
| `kind` | `STREAM`, `DISPLAY`, `RESULT`, `ERROR` |
| `stream_name` | `STREAM`이면 보통 `stdout` 또는 `stderr`, 그 외에는 `null` |
| `execution_count` | Jupyter execute result count 또는 `null` |
| `representations` | 동일 출력의 MIME별 실제 파일 목록 |
| `metadata` | Jupyter output metadata |
| `created_at` | 출력 레코드 수신 시각 |

| `kind` | Notebook 호환 의미 |
|---|---|
| `STREAM` | `stream` 출력 |
| `DISPLAY` | `display_data` 출력 |
| `RESULT` | `execute_result` 출력 |
| `ERROR` | `error` 출력 |

하나의 `DISPLAY` 또는 `RESULT`가 `text/plain`, `text/html`, `image/png`를 동시에 가지면
하나의 output 아래 representation이 세 개 존재한다.

## `outputs[].representations[]`

| 필드 | 의미 |
|---|---|
| `media_type` | `text/plain`, `application/json`, `image/png` 등의 MIME type |
| `encoding` | Runtime에서 수신한 표현 인코딩: `UTF8` 또는 `BASE64` |
| `relative_path` | manifest가 있는 디렉터리 기준 실제 출력 파일 경로 |
| `size_bytes` | 저장된 출력 파일 byte 크기 |
| `checksum_sha256` | 저장된 출력 파일 SHA-256 |
| `complete` | 개별 representation 파일 기록 완료 여부. terminal manifest에서는 `true` |
| `truncated_in_preview` | 원본을 잘랐는지. 현재 원본 파일은 자르지 않으므로 `false` |
| `metadata` | representation metadata |

`encoding=BASE64`는 Runtime에서 base64로 수신했다는 의미다. Executor는 저장 전에 이를 실제
binary로 디코딩하므로 `relative_path`의 이미지/PDF 파일을 다시 base64 decode하면 안 된다.
Agent가 LLM에 이미지를 전달하려면 저장된 binary 파일을 읽어 사용하는 Agent 도구 규격에
맞춰 인코딩한다.

경로 기준은 다음처럼 서로 다르다.

```text
source.relative_path
  -> Agent SHARED_STORAGE_ROOT 기준

outputs[].representations[].relative_path
  -> manifest.json이 있는 디렉터리 기준
```

## `output_summary`

| 필드 | 의미 |
|---|---|
| `output_count` | 출력 레코드 수 |
| `output_types` | `stream`, `display_data`, `execute_result`, `error` 종류별 개수 |
| `stream_names` | 발견된 stdout/stderr 이름 목록 |
| `mime_types` | 모든 representation의 MIME type 목록 |
| `has_image` | 이미지 representation 존재 여부 |
| `image_count` | 이미지 representation 개수 |
| `has_error` | `ERROR` output 존재 여부 |

## Agent 검증 및 읽기 순서

1. Result API가 반환한 `result_ref.relative_path`를 Agent 공유 PV 루트 아래에서 해석한다.
2. 절대경로, `..`, 공유 루트 이탈 경로는 거부한다.
3. manifest bytes의 크기와 SHA-256을 계산해 각각 `result_ref.size_bytes`,
   `result_ref.checksum_sha256`과 비교한다.
4. `schema_version`이 지원하는 `1.0`인지 확인한다.
5. manifest `identity`가 Result API의 Execution/Step/Attempt/fence와 일치하는지 확인한다.
6. `state`와 `complete`를 확인한다. `ABORTED` 출력은 부분 증거로만 사용한다.
7. source는 공유 PV 루트 기준, output representation은 manifest 디렉터리 기준으로 읽는다.
8. 각 파일의 `size_bytes`와 `checksum_sha256`을 검증한다.
9. 필요한 MIME representation만 Agent/LLM에 전달한다.

일반 파일 탐색으로 다른 attempt나 fencing generation을 선택해서는 안 된다. PostgreSQL 원본
상태가 가리키는 `result_ref`와 그 manifest 내부에 선언된 파일만 읽는다.
