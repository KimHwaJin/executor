# Executor용 Jupyter 독립 배포 패키지

**이 폴더만 전달받아 이미지 빌드가 가능하다.** 원본 저장소나 Executor 코드, 테스트
하네스는 필요 없다. JupyterLab + basic/ml 커널 + Executor 커스텀 확장을 포함한다.
Compose나 Kubernetes 매니페스트는 포함하지 않으며 배포 담당자가 플랫폼에 맞춰 작성한다.

## 포함 파일

| 파일/디렉토리 | 용도 |
|---|---|
| `Dockerfile`, `.dockerignore` | 이 폴더를 컨텍스트로 이미지 빌드 |
| `environments/server/requirements.txt` | Jupyter 서버 Python 3.12 패키지 |
| `environments/basic/requirements.txt` | basic 커널 Python 3.11 분석 패키지 |
| `environments/ml/requirements.txt` | ml 커널 Python 3.12 분석·ML 패키지 |
| `extension/pyproject.toml`, `extension/src/` | 자원 조회, 작업공간, 노트북 작성, 파일 다운로드 확장 |
| `executor_resource_extension.json` | 확장 활성화 |
| `jupyter_server_config.py` | 포트·루트·토큰·커널 설정 |
| `start-jupyter.sh` | 이미지 시작 명령 |
| `.env.example` | 로컬 실행용 환경변수 예시. 실제 토큰 없음 |
| `package.py` | 허용된 파일만 ZIP으로 다시 묶는 선택 도구(Python 3.10+) |
| `SOURCE.md` | 기준 소스 버전·유지보수 안내 |

## 1. 이미지 빌드

압축 해제 후 **Dockerfile이 있는 폴더에서** 실행한다.

```shell
docker build -t executor-jupyter:delivery .
```

Linux amd64 운영 노드용으로 다른 아키텍처에서 빌드한다면 명시적으로 지정한다.

```shell
docker build --platform linux/amd64 -t executor-jupyter:delivery .
```

빌드에는 `python:3.12-slim-bookworm` 이미지, Debian apt 저장소 및 Python 패키지 인덱스 접근이
필요하다. **완전 오프라인 패키지가 아니다.** 폐쇄망에서는 CI 빌더의 사내 이미지 미러,
apt 미러, Nexus/pip 설정을 먼저 준비한다. 비밀번호·인증 토큰을 Dockerfile, requirements,
이미지 레이어에 적지 않는다.

별도의 이미지 검증 단계나 uv/Conda/Mamba 의존성은 없다. 필요한 라이브러리는 각
`requirements.txt`에서 조정한다. ml 목록은 `-r ../basic/requirements.txt`로 basic 목록도
설치하므로 상대 디렉토리 구조를 유지한다.

## 2. Harbor에 업로드

아래 주소와 태그는 실제 환경 값으로 바꾼다.

```shell
docker tag executor-jupyter:delivery harbor.example.com/team/executor-jupyter:delivery
docker login harbor.example.com
docker push harbor.example.com/team/executor-jupyter:delivery
```

운영에서는 변경을 식별할 수 있는 고정 태그를 사용하고, 이미지 pull 인증은 플랫폼에 설정한다.

## 3. 배포 필수 설정

| 항목 | 설정 |
|---|---|
| 프로세스 | 이미지 ENTRYPOINT 그대로 사용. 별도 커맨드로 덮어쓰지 않음 |
| 컨테이너 포트 | `8888` |
| replicas | 등록할 Jupyter 서버별 `1` |
| 내부 Service | Executor가 접근할 수 있는 고유 주소 |
| `JUPYTER_TOKEN` | 필수. 플랫폼 Secret으로 주입. 빈 값이면 시작하지 않음 |
| `JUPYTER_ROOT_DIR` | 기본 `/workspace/pv`. 실제 PVC 마운트 경로와 일치 |
| 실행 사용자 | UID/GID `1000:1000` |
| 스토리지 권한 | 위 사용자가 루트 아래에 디렉토리/파일을 생성·수정할 수 있어야 함 |

여러 Jupyter 서버는 **동일한 공유 스토리지의 동일한 작업공간**을 바라보도록 배포한다.
각 서버 주소는 서로 달라야 한다. 여러 독립 Pod를 하나의 일반 Service로 무작위 분산하면
서버별 커널 세션을 안정적으로 찾을 수 없으므로, 서버별 Deployment/Service를 분리한다.

이 PV는 Jupyter의 노트북·아티팩트용이며 Agent/Executor의 코드·결과 공유 PV와는 별개다.
Executor가 Jupyter PV를 직접 마운트하거나 물리 경로를 알아야 하는 것은 아니다.

`HOME=/home/jovyan`은 설정·사용자 데이터 경로이고 실행 작업공간 루트와 다르다.
read-only root filesystem을 별도로 강제한다면 `/home/jovyan`과 `/tmp` 등 Jupyter가 쓰는
경로의 쓰기 가능 볼륨도 준비해야 한다. 일반적인 이미지 기본 실행에는 추가 설정이 필요 없다.
단, `/home/jovyan` 전체를 빈 볼륨으로 덮으면 내장 `.jupyter/jupyter_server_config.py`도
가려진다. 해당 설정을 보존하거나 볼륨에 미리 복사하는 절차 없이 홈 전체를 덮지 않는다.

## 4. 연결 확인 / Executor 등록

내부 주소 예: `http://jupyter-01.<namespace>.svc.cluster.local:8888`.
토큰을 환경변수로 준비한 뒤 인증 헤더를 포함해 조회한다.

```shell
curl --fail --header "Authorization: token ${JUPYTER_TOKEN}" http://127.0.0.1:8888/api/kernelspecs
curl --fail --header "Authorization: token ${JUPYTER_TOKEN}" http://127.0.0.1:8888/executor/resource-status
```

첫 API에는 `basic`, `ml`이 있어야 하고 기본 커널은 basic이다. 두 번째는 서버 프로세스에서
cgroup 자원을 읽는 커스텀 확장으로, 별도 모니터링 커널을 만들지 않는다.
Windows PowerShell에서는 `curl.exe`와 `$env:JUPYTER_TOKEN` 표기를 사용한다.

Executor의 런타임 등록 API에 이름, Jupyter endpoint, 토큰, 사용 풀을 등록한다.
WebSocket 연결과 긴 실행/다운로드를 위한 네트워크·타임아웃 설정도 허용해야 한다.
이 패키지의 커스텀 API를 사용하는 최신 Executor와 함께 배포한다.

## 5. 선택: 로컬 Docker 실행

`.env.example`을 `.env`로 복사하고 비어 있는 토큰을 채운 뒤 실행한다. Docker named volume을
사용하는 예이며 운영에서는 플랫폼의 공유 PVC를 마운트한다.

```shell
docker volume create executor-jupyter-workspace
docker run --rm --name executor-jupyter-delivery --env-file .env -p 127.0.0.1:8888:8888 --mount source=executor-jupyter-workspace,target=/workspace/pv executor-jupyter:delivery
```

## 6. 다시 압축해서 전달하기

이 폴더에서 실행한다(Python 표준 라이브러리만 사용).

```shell
python package.py
```

`dist/executor-jupyter.zip`과 `dist/executor-jupyter.zip.sha256`이 생성된다. ZIP 내부의
`executor-jupyter/` 폴더를 꺼내면 같은 방식으로 바로 빌드할 수 있다.
도구는 허용된 소스·설정 파일만 포함하고 실제 `.env`, 작업 데이터, 캐시, 기존 ZIP,
가상환경은 포함하지 않는다. 심볼릭 링크를 통한 외부 파일 포함도 거부한다.

단, 허용된 소스 파일 안에 직접 작성한 비밀값까지 자동 판별하는 도구는 아니다.
소스에는 비밀값을 넣지 말고, 전달할 때 실제 토큰이나 운영 데이터를 함께 보내지 않는다.
