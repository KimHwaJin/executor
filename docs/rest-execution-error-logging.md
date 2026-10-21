# REST Execution 요청 오류 로그

Execution 경로(`/api/v1/executions` 및 하위 경로)의 POST/PUT/PATCH/DELETE 요청이
4xx/5xx로 실패하면 `rest.execution.request_failed` JSON 로그를 ERROR로 기록한다.
정상 요청, GET, MCP 및 Runtime 관리 API는 대상이 아니다. 응답 스키마와 DB는 바꾸지 않는다.

- 로거: `executor_service.interfaces.http.request_error_logging`
- `REST_ERROR_BODY_LOGGING_ENABLED`: 기본 false. 원문 기록을 명시적으로 허용한
  환경에서만 true로 설정하고 서비스를 재시작한다.
- `REST_ERROR_BODY_LOG_MAX_BYTES=65536`: 요청 body 로그 최대 크기(기본 64KiB).
- 메서드, 경로, 로그용 request_id, 상태 코드, 요청 body와 오류 응답을 기록한다.
- 오류 응답은 최대 16KiB, 미처리 예외는 타입·메시지를 기록한다.
- request_id는 이 로그의 식별자이며 Agent 요청 ID나 응답 헤더는 아니다.

요청 body는 마스킹하지 않는다. 코드·토큰·개인정보도 포함될 수 있으므로 로그 접근권한과
보관정책을 별도로 관리한다. Authorization 등 요청 헤더는 수집하지 않는다.
JSON 문자열로 기록하여 줄바꿈을 escape하며 잘못된 UTF-8은 대체 문자로 표시한다.

실제 API가 읽은 요청 청크만 복사하고 원래 요청은 그대로 전달한다. 로그 제한을 넘겨도
실제 요청 body를 자르지 않는다. `body_observed_bytes`는 관측한 크기,
`body_complete`는 끝까지 읽었는지, `body_truncated`는 로그 크기 제한 초과 여부다.
라우팅 단계처럼 body를 읽기 전에 실패한 경우 원문이 비어 있고 complete=false일 수 있다.
공유 스토리지 PATH 입력은 전달받은 경로만 기록하며 해당 파일 내용은 별도로 읽지 않는다.

내부 로깅 설정에서도 위 로거 또는 `executor_service`의 ERROR 로그를 허용해야 한다.
