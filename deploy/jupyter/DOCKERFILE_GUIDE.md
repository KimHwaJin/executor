# Jupyter Dockerfile 상세 가이드

이 문서는 이 디렉토리만 독립적인 이미지 소스 패키지로 분리하여 Git에서 관리하고,
Jenkins에서 빌드한 이미지를 Harbor에 업로드하는 상황을 기준으로 설명한다.

Dockerfile의 각 블록이 왜 필요한지와 변경 시 확인할 항목을 함께 정리한다. 줄 번호는 현재
Dockerfile 기준이며 Dockerfile 변경 시 달라질 수 있다.

## 1. 전체 빌드 구조

이미지는 두 단계로 만들어진다.

1. uv와 Python 3.10이 포함된 Bookworm Slim 이미지에서 독립적인 `3102311` 커널
   환경을 만든다.
2. uv와 Python 3.11이 포함된 Bookworm Slim 최종 이미지에 Jupyter 서버, `default`
   커널, Python 3.10 런타임, `3102311` 커널과 Executor 연동 확장을 조립한다.

세 Python 환경은 각각 독립된 `pyproject.toml`과 `uv.lock`으로 관리한다.

| 환경 | Python | 이미지 내부 경로 | 역할 |
|---|---:|---|---|
| `server` | 3.11 | `/opt/venvs/jupyter` | JupyterLab·Jupyter Server 실행 |
| `default` | 3.11 | `/opt/venvs/default` | 기본 분석 커널 |
| `3102311` | 3.10.x | `/opt/venvs/3102311` | 별도 승인 패키지 커널 |

`3102311`이라는 이름은 기존 Runtime Profile 계약을 위한 kernelspec ID다. Python patch
버전을 의미하지 않으며 실제 patch 버전은 반입한 `PYTHON310_IMAGE`가 결정한다.

## 2. uv와 패키지 인덱스

### 1~3행: Python별 uv 이미지와 기본 패키지 인덱스

```dockerfile
ARG PYTHON310_IMAGE=ghcr.io/astral-sh/uv:0.12.8-python3.10-bookworm-slim
ARG PYTHON311_IMAGE=ghcr.io/astral-sh/uv:0.12.8-python3.11-bookworm-slim
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
```

- 두 이미지는 동일한 uv 버전과 Debian 12 Bookworm Slim을 사용한다.
- 폐쇄망에서는 두 이미지를 사내 Harbor에 반입하고 각 빌드 인자를 덮어쓴다.
- `UV_DEFAULT_INDEX`는 lock 검증과 패키지 설치에 사용할 PEP 503 인덱스다.
- 저장소의 최초 lock과 기본 인덱스는 공개 PyPI로 일치시켜 템플릿 자체를 검증 가능하게
  한다. 폐쇄망 반입 후에는 실제 Nexus 주소로 세 lock을 먼저 갱신하고 같은 주소를 빌드
  인자로 전달한다.
- lock을 생성할 때 사용한 인덱스와 Docker 빌드 인덱스가 달라지면 `--locked` 검증이
  실패할 수 있다. 세 환경의 lock과 이미지 빌드는 동일한 Nexus를 사용해야 한다.
- 계정·비밀번호·토큰은 Dockerfile이나 빌드 인자 URL에 작성하지 않는다.

## 3. Python 3.10 커널 스테이지

### Python 3.10 Bookworm Slim 베이스

```dockerfile
FROM ${PYTHON310_IMAGE} AS python310
```

- `3102311` 커널은 이미지에 포함된 Python 3.10.x를 사용한다.
- 최종 Python 3.11 이미지와 동일한 Debian 12 Bookworm 계열을 사용한다.
- 이 스테이지는 빌드용이며 최종 컨테이너의 시작 이미지가 아니다.

### 인덱스와 uv 설치 정책

```dockerfile
ARG UV_DEFAULT_INDEX

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
```

- 전역 `ARG`는 각 `FROM` 뒤에 다시 선언해야 해당 스테이지의 `RUN`에서 사용할 수 있다.
- uv는 베이스 이미지에 이미 설치되어 있어 별도의 uv 복사나 설치가 필요 없다.
- `UV_COMPILE_BYTECODE=1`은 설치 시 `.pyc`를 미리 생성하여 초기 import 지연을 줄인다.
- `UV_LINK_MODE=copy`는 컨테이너 레이어와 가상환경 사이의 hardlink 경고를 방지한다.
- `UV_NO_CACHE=1`은 uv 다운로드 캐시를 최종 이미지 레이어에 남기지 않는다.

### 13~18행: 3102311 프로젝트 동기화

```dockerfile
COPY environments/3102311 /opt/jupyter-env/3102311

RUN UV_PROJECT_ENVIRONMENT=/opt/venvs/3102311 \
    uv sync --project /opt/jupyter-env/3102311 \
        --locked --no-dev --no-install-project \
        --python /usr/local/bin/python3.10
```

- 환경 디렉토리 전체를 복사하므로 `pyproject.toml`과 `uv.lock`이 모두 빌드 입력이다.
- `UV_PROJECT_ENVIRONMENT`는 기본 `.venv` 대신 고정된 이미지 경로에 환경을 만든다.
- `--project`는 사용할 독립 uv 프로젝트를 명시한다.
- `--locked`는 lock이 누락되거나 `pyproject.toml`과 일치하지 않으면 빌드를 실패시킨다.
- `--no-dev`는 운영 환경에 개발 의존성을 설치하지 않는다.
- `--no-install-project`는 환경 정의용 가상 프로젝트 자체를 패키지로 설치하지 않는다.
- `--python`은 해당 이미지의 Python 3.10 인터프리터를 사용하게 한다.
- `ipykernel`도 이 환경의 직접 의존성에 포함되어 lock으로 관리된다.

## 4. 최종 Python 3.11 이미지

### 최종 Python 3.11 Bookworm Slim 베이스

```dockerfile
FROM ${PYTHON311_IMAGE}
ARG UV_DEFAULT_INDEX
```

- 이 스테이지가 최종 Harbor 이미지가 된다.
- Jupyter 서버와 `default` 커널은 Python 3.11을 사용한다.
- uv가 베이스 이미지에 포함되므로 Jupyter 터미널에서 `uv --version`을 실행할 수 있다.
- 표준 커널 경로는 root 소유이므로 일반 사용자에게 이미지 내 환경 변경 권한은 없다.
- 폐쇄망에서 사용자가 PVC 아래 별도 환경을 만들게 허용한다면 Deployment에도 런타임
  `UV_DEFAULT_INDEX`를 Nexus 주소로 주입한다. Docker 빌드 인자는 런타임에 남지 않는다.

### 24~29행: 배포 기본값

```dockerfile
ENV JUPYTER_ROOT_DIR=/workspace/jupyter \
    JUPYTER_TOKEN=default
```

- `JUPYTER_ROOT_DIR`은 노트북과 아티팩트가 저장될 공유 PVC 마운트 경로다.
- `JUPYTER_TOKEN=default`는 기본 시험값이며 운영에서는 Kubernetes Secret으로 덮어쓴다.
- Dockerfile 기본값을 바꾸지 않아도 컨테이너 환경변수가 우선한다.

### 31~38행: 공통 런타임 설정

```dockerfile
ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venvs/jupyter/bin:${PATH}" \
    HOME=/home/jovyan \
    JUPYTER_CONFIG_DIR=/home/jovyan/.jupyter \
    JUPYTER_PATH=/opt/venvs/jupyter/share/jupyter \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
```

- `PATH`는 Jupyter 서버 환경의 실행 파일을 기본으로 선택한다.
- `HOME`은 Jupyter 설정과 사용자 런타임 파일의 기준 경로다.
- `JUPYTER_CONFIG_DIR`은 서버 설정 파일 위치를 고정한다.
- `JUPYTER_PATH`는 등록한 kernelspec을 Jupyter 서버가 발견하게 한다.
- uv 설정은 앞 스테이지와 동일한 설치 정책을 유지한다.

### 40~47행: OS 런타임 패키지

```dockerfile
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ... \
        tini \
        ... \
    && rm -rf /var/lib/apt/lists/*
```

- 두 Python 이미지에 공통으로 필요한 최소 런타임 패키지를 설치한다.
- `ca-certificates`는 HTTPS 통신에 사용한다.
- `curl`은 Docker Compose 및 운영 진단에서 Jupyter HTTP 상태를 확인할 때 사용한다.
- `fonts-dejavu-core`는 Matplotlib 등에서 기본 글꼴을 제공한다.
- `libgomp1`은 NumPy, SciPy 등 병렬 네이티브 코드에서 사용될 수 있다.
- `tini`는 Jupyter 하위 커널 프로세스의 signal 전달과 zombie process 회수를 담당한다.
- `--no-install-recommends`와 apt 목록 삭제로 불필요한 이미지 크기를 줄인다.
- 폐쇄망에서는 별도 승인된 apt mirror 설정이 필요하다.

Python 3.10과 3.11 스테이지는 모두 Debian 12 Bookworm Slim이어야 한다. 한쪽 이미지만
다른 Debian 세대로 변경하면 Python 런타임과 네이티브 패키지 호환성을 다시 검증해야 한다.

## 5. Python 3.10 환경 결합

### 48~51행: 인터프리터와 환경 복사

```dockerfile
COPY --from=python310 /usr/local/bin/python3.10 /usr/local/bin/python3.10
COPY --from=python310 /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.10.so.1.0
COPY --from=python310 /usr/local/lib/python3.10 /usr/local/lib/python3.10
COPY --from=python310 /opt/venvs/3102311 /opt/venvs/3102311
```

- Python 실행 파일만이 아니라 공유 라이브러리와 표준 라이브러리도 함께 복사한다.
- 마지막 줄은 lock으로 설치된 독립 `3102311` 환경을 최종 이미지에 포함한다.
- 두 베이스 이미지의 Debian 계열을 임의로 다르게 바꾸면 네이티브 라이브러리 호환성이
  깨질 수 있으므로 함께 검토해야 한다.

## 6. Jupyter 서버와 default 환경

### 53~64행: 두 독립 프로젝트 동기화

```dockerfile
COPY environments/server /opt/jupyter-env/server
COPY environments/default /opt/jupyter-env/default

RUN ldconfig \
    && UV_PROJECT_ENVIRONMENT=/opt/venvs/jupyter \
        uv sync --project /opt/jupyter-env/server \
            --locked --no-dev --no-install-project \
            --python /usr/local/bin/python3.11 \
    && UV_PROJECT_ENVIRONMENT=/opt/venvs/default \
        uv sync --project /opt/jupyter-env/default \
            --locked --no-dev --no-install-project \
            --python /usr/local/bin/python3.11
```

- `ldconfig`는 앞에서 복사한 Python 3.10 공유 라이브러리를 시스템 linker cache에 반영한다.
- `server`와 `default`는 같은 Python 3.11을 쓰지만 가상환경과 의존성 lock은 분리된다.
- Jupyter 서버 패키지 변경이 분석 커널 패키지를 암묵적으로 변경하지 않는다.
- `default`의 `ipykernel`도 해당 프로젝트에서 직접 관리한다.

## 7. Executor 연동 확장과 kernelspec

### 66~68행: 확장 소스와 활성화 설정

```dockerfile
COPY extension /opt/jupyter-resource-extension
COPY executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json
```

- 확장은 Executor가 사용하는 자원 조회, 작업공간 준비, 노트북 반영, 파일 다운로드 API를
  제공한다.
- JSON 파일은 Jupyter Server가 확장을 자동 활성화하게 한다.

### 70~80행: 확장 설치와 커널 등록

```dockerfile
RUN uv pip install --strict --python /opt/venvs/jupyter/bin/python \
        --no-deps /opt/jupyter-resource-extension \
    && ... ipykernel install --name=default ... \
    && ... ipykernel install --name=3102311 ... \
    && rm -rf /opt/venvs/jupyter/share/jupyter/kernels/python3
```

- 확장은 서버 환경에만 설치한다.
- `--no-deps`는 서버 lock 밖에서 확장 설치가 의존성 버전을 바꾸지 못하게 한다.
- 각 커널은 자신의 Python으로 kernelspec을 등록한다.
- 자동 생성된 `python3` kernelspec은 제거하여 허용 커널을 `default`, `3102311`로 제한한다.

## 8. 비-root 사용자와 PVC 권한

### 82~91행: UID/GID 1000 사용자 생성

```dockerfile
RUN rm -rf /home/jovyan \
    && groupadd --gid 1000 jovyan \
    && useradd --uid 1000 --gid 1000 --create-home ... jovyan \
    && mkdir -p "${JUPYTER_ROOT_DIR}" /home/jovyan/.jupyter \
    && chown -R 1000:1000 "${JUPYTER_ROOT_DIR}" /home/jovyan
```

- Jupyter 프로세스는 root가 아닌 `1000:1000`으로 실행한다.
- Kubernetes PVC도 이 사용자가 디렉토리와 파일을 생성·수정할 수 있어야 한다.
- 이미지의 `chown`은 PVC가 마운트되면 가려질 수 있으므로 실제 PV 권한은 배포 환경에서
  별도로 보장해야 한다.

### 93~98행: 설정, 시작 스크립트와 실행 사용자

```dockerfile
COPY --chown=1000:1000 jupyter_server_config.py ...
COPY --chmod=755 start-jupyter.sh /usr/local/bin/start-jupyter

USER 1000:1000
WORKDIR "${JUPYTER_ROOT_DIR}"
```

- 설정 파일 소유자를 런타임 사용자로 지정한다.
- 시작 스크립트에 실행 권한을 부여한다.
- `USER` 이후 Jupyter와 커널은 비-root로 실행된다.
- `WORKDIR`은 셸과 상대 경로 실행의 기본 위치이며 실제 저장 루트와 일치한다.

## 9. 포트와 시작 프로세스

### 100~102행

```dockerfile
EXPOSE 8888
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-jupyter"]
```

- `EXPOSE`는 이미지 메타데이터이며 Kubernetes Service를 자동 생성하지 않는다.
- Deployment와 Service의 `containerPort`, `targetPort`는 8888에 맞춘다.
- `tini`가 종료 신호를 Jupyter와 커널에 전달한다.

## 10. 의존성 변경 규칙

환경별 파일은 다음 두 개를 한 쌍으로 관리한다.

```text
environments/<환경>/pyproject.toml
environments/<환경>/uv.lock
```

패키지 추가 예시:

```shell
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv add --project environments/default "xgboost==3.0.5"
```

직접 `pyproject.toml`을 수정했다면 lock을 갱신한다.

```shell
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/default
```

검증만 할 때는 다음을 사용한다.

```shell
UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
uv lock --project environments/default --check
```

- `uv.lock`은 사람이 수정하지 않는다.
- `pyproject.toml`만 변경하거나 `uv.lock`만 변경한 커밋은 허용하지 않는다.
- 이미지 빌드 중 lock을 갱신하지 않는다.
- 세 환경은 독립 프로젝트이므로 변경한 환경의 lock만 갱신한다.
- 모든 Jupyter 서버는 같은 image tag보다 가능하면 같은 image digest를 사용한다.

## 11. 폐쇄망 Jenkins 빌드

이 디렉토리 자체를 Git 저장소 또는 Jenkins checkout 루트로 사용할 수 있다.

```shell
docker build \
  --build-arg PYTHON310_IMAGE=harbor.example.com/library/uv:0.12.8-python3.10-bookworm-slim \
  --build-arg PYTHON311_IMAGE=harbor.example.com/library/uv:0.12.8-python3.11-bookworm-slim \
  --build-arg UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
  --tag harbor.example.com/analytics/jupyter-runtime:${BUILD_NUMBER} \
  .

docker push harbor.example.com/analytics/jupyter-runtime:${BUILD_NUMBER}
```

빌드 전에 확인할 외부 의존성은 다음과 같다.

| 대상 | 준비 사항 |
|---|---|
| Python 3.11 + uv Bookworm 이미지 | Harbor에 반입하고 `PYTHON311_IMAGE`로 지정 |
| Python 3.10 + uv Bookworm 이미지 | Harbor에 반입하고 `PYTHON310_IMAGE`로 지정 |
| PyPI 패키지 | Nexus group/proxy 저장소와 lock 생성 시 사용한 동일 URL |
| Debian 패키지 | 승인된 apt mirror 또는 접근 가능한 Debian 저장소 |
| 비밀값 | Dockerfile과 Git이 아니라 Jenkins credential/배포 Secret으로 관리 |

두 이미지 빌드 인자는 Dockerfile을 직접 수정하지 않고 Jenkins에서 사내 Harbor 주소로
덮어쓴다. 가능하면 승인된 image digest도 함께 고정한다.

## 12. 변경 시 함께 확인할 항목

| 변경 | 반드시 같이 확인할 내용 |
|---|---|
| Python 3.10/3.11 이미지 | Debian 계열, 표준 라이브러리 복사 경로, 네이티브 패키지 import |
| `pyproject.toml` | 같은 환경의 `uv.lock` 갱신·커밋 |
| Nexus 주소 | 세 lock의 registry 출처와 Docker `UV_DEFAULT_INDEX` 일치 |
| 커널 이름 | kernelspec 등록, Jupyter 허용 목록, Executor runtime profile |
| UID/GID | PVC 소유권, `securityContext`, nbviewer 읽기 권한 |
| 작업공간 루트 | PVC mountPath, `JUPYTER_ROOT_DIR`, Executor 등록 정보 |
| 확장 코드 | 확장 단위 테스트와 실제 Jupyter API 검증 |
| 시작 명령 | `tini` signal 전달 및 정상 종료 여부 |
