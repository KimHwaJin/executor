# GET Artifact Content — 현재 파일 다운로드 / BFF 연동

## 용도와 호출 구조

`GET /api/v1/artifacts/{artifact_id}/content`

등록된 PV Artifact의 **현재 저장된 파일**을 다운로드한다. 노트북, 리포트, 이미지 등
파일 종류에 공통으로 사용한다. 원본 실행 코드·출력을 담은 Step result manifest 조회와는
다르다. 사람이 노트북을 수정해도 원래 Step 실행 증거와 Redis 이벤트는 바뀌지 않는다.

호출: 사용자 → BFF → Executor → Jupyter 커스텀 파일 API.
본문: Jupyter PV → Jupyter → Executor → BFF → 사용자.

Executor/BFF가 Jupyter PV를 마운트할 필요는 없다. Jupyter 토큰과 내부 경로는 서버 간에만
사용하고, 클라이언트는 `artifact_id`로 요청한다. BFF에서 인증 및 다운로드 권한을 확인한다.
이 API는 REST 전용이며 MCP로 바이너리 전체를 반환하지 않는다. S3는 현재 미지원이다.

## 요청

| 위치 | 이름 | 필수 | 의미 |
|---|---|---|---|
| Path | `artifact_id` | O | 등록된 Artifact UUID |
| Header | `Range` | X | 단일 바이트 범위. 생략하면 현재 파일 전체 |

본문은 없다. 범위는 0부터 시작하고 끝 번호도 포함한다.

```http
GET /api/v1/artifacts/{artifact_id}/content
```

```http
GET /api/v1/artifacts/{artifact_id}/content
Range: bytes=0-1048575
```

`bytes=1048576-`(해당 위치부터 끝), `bytes=-1048576`(마지막 구간)도 지원한다.
쉼표로 여러 범위를 한 요청에 넣는 multipart Range는 지원하지 않는다.
HEAD 및 If-Range/조건부 다운로드 지원은 이 API 계약에 포함되지 않는다.

## 응답

| 상태 | 의미 |
|---|---|
| `200` | Range 생략: 현재 파일 전체. 빈 파일도 정상이며 길이는 0 |
| `206` | 유효한 Range: 해당 구간. 끝 번호가 EOF를 넘으면 실제 끝까지 |
| `416` | 지원하지 않거나 만족할 수 없는 Range. 현재 파일 크기를 응답 |

성공 본문은 **파일 바이트 그 자체**이며 JSON으로 감싸거나 base64로 변환하지 않는다.

| Header | 의미 |
|---|---|
| `Content-Type` | Artifact의 미디어 타입 |
| `Content-Disposition` | 첨부파일과 UTF-8 파일명 |
| `Content-Length` | 이번 응답 본문의 바이트 수. 206이면 구간 크기 |
| `Accept-Ranges` | `bytes` |
| `Content-Range` | 206: `bytes 시작-끝/전체크기`, 416: `bytes */전체크기` |
| `ETag` | 이번에 연 전체 파일의 SHA-256을 큰따옴표로 감싼 값 |
| `X-Checksum-SHA256` | 이번에 연 **전체 파일**의 SHA-256. 부분 본문의 해시가 아님 |
| `Cache-Control` | `no-store, no-transform` |

예: 1,000바이트 파일 중 처음 100바이트 요청 시 `Content-Length: 100`,
`Content-Range: bytes 0-99/1000`이다.

## DB 메타데이터와 현재 파일의 차이

Artifact 목록/상세의 `size_bytes`, `checksum_sha256`은 등록 시점에 관찰한 정보다.
같은 경로에 노트북이 재작성되거나 사람이 저장하면 현재 파일과 달라질 수 있다.
다운로드는 그 값을 읽기 제한이나 체크섬으로 사용하지 않는다. 값이 누락되어 있어도
등록 상태·경로가 유효하고 실제 파일을 읽을 수 있으면 다운로드할 수 있다.

다운로드 GET으로 Artifact 생성 이력·감사필드·Execution 상태·Step result ref를 갱신하지
않는다. 과거 Artifact ID도 현재 그 경로의 파일을 가리키며 과거 버전 복원 API가 아니다.
리포트의 `append_to_notebook` 기본값은 계속 `false`다. 명시적으로 `true`로 추가한 후에는
같은 노트북 Artifact 다운로드가 추가된 내용까지 반환한다.

## 저장 중 다운로드의 보장 범위

Jupyter에서 파일을 먼저 열고, 같은 파일 핸들로 크기 확인·체크섬 계산·본문 읽기를 한다.
익스큐터의 노트북 저장은 임시파일을 완성한 뒤 경로를 원자적으로 교체한다. POSIX에서는
기존 다운로드가 열었던 파일을 끝까지 읽고, 다음 다운로드는 새 파일을 읽는다.

이 방식은 **모든 외부 쓰기에 대한 스냅샷 보장은 아니다**. Jupyter 기본 저장이나 분석
함수가 같은 파일을 직접 덮어쓰면 다를 수 있다. 크기/수정시각 변화와 전송 구간 해시
불일치를 감지하지만 임의 동시 쓰기를 완벽히 차단하지 않는다. 이런 파일은 저장 완료 후
다운로드한다. Windows 파일 교체에 POSIX와 같은 보장을 가정하지 않는다.

여러 Range 요청은 각각 파일을 연다. 모든 조각의 ETag·전체 크기가 같은지 확인한 뒤
합치고 전체 SHA-256을 검증한다. 다르면 조각을 섞지 말고 저장 완료 후 처음부터 다시 받는다.

## 오류 및 연결 관리

- 없는 Artifact, 사용 불가 상태, 미지원 저장소는 기존 도메인 오류 응답을 사용한다.
- 파일이 없어졌거나 준비 중 변경을 감지하면 본문 전송 전에 사용 불가 오류를 반환한다.
  준비 중 변경은 응답 전까지 한 번만 새 파일 핸들로 재시도한다. 저장/공유 마운트의
  메타데이터 갱신이 끝나면 정상 제공하고, 계속 변경되면 오류로 종료한다. 전송 중 재시도는 없다.
- `416`은 JSON 오류와 `Content-Range: bytes */<현재 크기>`를 함께 반환한다.
- 스트리밍 시작 후 읽기 실패는 기존 파일 응답에 JSON을 덧붙이지 않고 연결을 중단한다.
  이미 보낸 HTTP 상태를 새 오류 상태로 바꿀 수 없으므로 BFF/클라이언트는 전송 완료 여부,
  길이와 해시를 확인해야 한다. 불완전한 파일을 정상 다운로드로 취급하지 않는다.
- 대상 서버 장애의 우회는 응답 메타데이터를 확정하기 전까지만 가능하다. 이후에는 다른
  서버/새 파일을 이어 붙이지 않는다.
- 완료, 사용자 취소, 전송 예외 모두에서 내부 HTTP 응답·드라이버 연결·파일 핸들을 정리한다.

## BFF 구현 체크리스트

1. HTTP 클라이언트도 스트리밍 모드로 호출한다. `.content`, `.read()` 등으로 전체 파일을
   먼저 버퍼링하지 않는다. 다운로드 크기에 비례하는 전체 메모리 버퍼/임시파일은 필요 없다.
2. 사용자 `Range`를 Executor에 전달한다. 내부 요청은 `Accept-Encoding: identity`로 하고
   본문을 재압축/압축해제/JSON 변환하지 않는다.
3. 위 다운로드 응답 헤더와 `200/206/416` 등 상태를 보존한다. 내부 토큰이나 hop-by-hop
   헤더를 무조건 복사하지 않는다.
4. 최종 사용자에게 전송하는 동안 upstream 응답을 열어 둔다. 생성기만 반환하고 upstream
   컨텍스트를 먼저 닫으면 안 된다. 종료/예외/취소에서 upstream 응답도 반드시 닫는다.
5. 느린 클라이언트 때문에 무제한 큐를 쌓지 않는다. HTTP 연결 풀·동시 다운로드 수와
   BFF/Istio의 timeout, body 제한, buffering 정책을 함께 검증한다.
6. 전송 도중 실패한 응답을 자동으로 처음부터 재요청해 기존 바이트 뒤에 이어 붙이지 않는다.

## 성능과 배포

현재 강한 ETag/SHA-256 응답을 유지하기 위해 Jupyter가 **요청마다 전체 파일을 한 번 읽어
해시한 뒤**, 요청 구간을 다시 읽어 전송한다. Range도 전체 해시 계산 비용이 든다.
메모리는 청크 단위로 제한되지만, 첫 바이트까지의 지연과 스토리지 I/O는 파일 크기에 따라
증가한다. 큰 파일을 불필요하게 여러 조각으로 나누면 이 비용도 반복된다. 불변 파일 버전과
안전한 해시 캐시를 별도로 도입하기 전까지 이 특성을 고려한다. DB의 옛 해시를 재사용하지 않는다.

Executor와 Jupyter 확장/이미지를 함께 업데이트해야 한다. 내부 `start/end` query 방식은
제거되었고 `Range` 헤더로 통합됐다. 외부 Artifact URL과 Execution/Redis 스키마는 유지된다.
DB 마이그레이션, 기존 Artifact 삭제·재등록, PV 초기화는 필요 없다.

실제 Docker 재현 테스트(기존 Compose/DB/Redis를 변경하지 않음):

```shell
RUN_ARTIFACT_DOWNLOAD_LIVE=1 uv run pytest -q -s tests/test_artifact_download_live.py
```

PowerShell에서는 `$env:RUN_ARTIFACT_DOWNLOAD_LIVE = '1'` 설정 후 같은 `uv run pytest` 명령을
사용한다. 이 opt-in 테스트에는 Docker와 미리 빌드한 `executor-jupyter:local` 이미지가
필요하며, 일회용 Jupyter·SQLite DB·HTTP Executor/BFF를 사용하고 테스트 종료 시 정리한다.
