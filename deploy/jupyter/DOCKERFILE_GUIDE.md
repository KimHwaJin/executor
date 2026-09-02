# Jupyter Dockerfile 상세 가이드

이 문서는 [`Dockerfile`](Dockerfile)을 구문 단위로 설명한다. Docker 문법뿐 아니라 각
구문이 필요한 이유, 변경 시 영향, 폐쇄망 및 PVC 권한 주의사항을 함께 기록한다.

현재 이미지 구조는 다음과 같다.

- 패키지·가상환경 도구: uv 0.12.8
- Jupyter 서버: Python 3.11, `/opt/venvs/jupyter`
- `default` 커널: Python 3.11, `/opt/venvs/default`
- `3102311` 커널: 정확히 Python 3.10.11, `/opt/venvs/3102311`
- 컨테이너 실행 사용자: `jovyan`, UID/GID `1000:1000`
- 작업공간 기본 경로: `/workspace/jupyter`

> 줄 번호는 현재 Dockerfile 기준이다. Dockerfile 수정 후에는 실제 구문과 함께 다시
> 확인해야 한다.

## 1. 전체 빌드 구조

Dockerfile은 다음 세 스테이지를 사용한다.

1. `uv`: 고정 버전 uv 바이너리를 제공한다.
2. `python310`: Python 3.10.11의 `3102311` 커널 환경을 만든다.
3. 최종 스테이지: Python 3.11 Jupyter 서버와 `default` 커널을 만들고 3.10.11 환경을
   결합한다.

두 Python 이미지는 모두 Debian 11 bullseye를 사용한다. 동일한 OS ABI를 사용하므로
OpenSSL·libffi 호환 파일이나 별도 `LD_LIBRARY_PATH`가 필요하지 않다.

> Debian 11 bullseye는 2026년 8월 31일 LTS가 종료되었다. 프로젝트 결정에 따라 ABI
> 통일을 우선하여 사용하며, 운영에서는 조직의 이미지 스캔·보완 패치·Extended LTS
> 정책을 별도로 적용해야 한다.

## 2. uv 빌드 입력

### 1~2행: uv 이미지와 Python 패키지 인덱스

```dockerfile
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.8
ARG UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/
```

- `UV_IMAGE`는 uv와 uvx 바이너리를 가져올 이미지다.
- `0.12.8`로 고정하여 `latest` 변경으로 빌드 결과가 갑자기 달라지는 것을 방지한다.
- 폐쇄망에서는 공식 uv 이미지를 사내 Harbor에 미러링하고 `--build-arg UV_IMAGE=...`로
  덮어쓴다.
- `UV_DEFAULT_INDEX`는 모든 `uv pip install`이 사용하는 기본 PEP 503 패키지
  인덱스다.
- uv는 `pip.conf`나 `PIP_INDEX_URL`을 읽지 않으므로 이 빌드 인자를 사용한다.
- 예시 Nexus 주소는 실제 주소로 덮어써야 한다.
- 계정·비밀번호·토큰을 빌드 인자나 URL에 포함하지 않는다. 현재 구성은 신뢰된 내부
  네트워크의 익명 Nexus 접근을 전제로 한다.
- `ARG`는 최종 컨테이너의 런타임 환경변수로 자동 보존되지 않는다.

### 4행: uv 전용 스테이지

```dockerfile
FROM ${UV_IMAGE} AS uv
```

- uv 공식 distroless 이미지에 `uv`, `uvx` 실행 파일만 들어 있다.
- `AS uv`라는 이름으로 이후 스테이지가 `COPY --from=uv`를 사용할 수 있게 한다.
- 이 스테이지 자체는 최종 이미지에 포함되지 않는다.

## 3. Python 3.10.11 커널 스테이지

### 6행: 정확한 Python 3.10.11

```dockerfile
FROM python:3.10.11-slim-bullseye AS python310
```

- `3102311` 커널의 정확한 Python 패치 버전을 보장한다.
- `AS python310`은 최종 스테이지가 인터프리터와 환경을 복사할 때 사용한다.
- `3.10`처럼 느슨한 태그로 바꾸면 3.10.11 보장이 깨진다.

### 8행: 글로벌 빌드 인자 재선언

```dockerfile
ARG UV_DEFAULT_INDEX
```

- 첫 `FROM` 이전에 선언한 글로벌 ARG를 이 스테이지의 `RUN`에서 사용할 수 있게 다시
  선언한다.
- 이름이 uv 환경변수와 같기 때문에 `uv pip install`이 Nexus 주소를 자동으로 읽는다.
- `ENV`가 아니므로 완성된 커널 환경의 런타임 설정으로 남기지 않는다.

### 10행: uv 실행 파일 복사

```dockerfile
COPY --from=uv /uv /uvx /bin/
```

- uv 스테이지에서 두 실행 파일을 `/bin`에 복사한다.
- 설치 스크립트나 pip로 uv를 설치하지 않아 외부 다운로드와 부트스트랩 의존성을
  줄인다.
- `/bin`은 기본 PATH에 포함되므로 이후 `uv` 명령을 바로 사용할 수 있다.

### 12~14행: uv 빌드 동작

```dockerfile
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
```

- `UV_COMPILE_BYTECODE=1`: Python bytecode를 빌드 중 생성해 최초 import 지연을 줄인다.
- `UV_LINK_MODE=copy`: uv 캐시와 가상환경 사이에 hardlink를 만들지 않고 파일을
  복사하여 Docker 파일시스템 경계의 링크 경고를 방지한다.
- `UV_NO_CACHE=1`: uv 다운로드 캐시를 이미지 레이어에 남기지 않는다. 설치된 패키지는
  삭제되지 않는다.
- 이 스테이지는 최종 이미지가 아니므로 해당 ENV는 3.10 빌드 과정에만 적용된다.

### 16행: 3102311 패키지 목록

```dockerfile
COPY environments/3102311/requirements.txt /opt/jupyter-env/3102311/requirements.txt
```

- 분석가가 제공하는 Python 3.10.11 전용 패키지 목록을 복사한다.
- 현재 파일은 의도적으로 비어 있어 승인된 목록을 그대로 붙여넣을 수 있다.
- `requirements.txt`는 계속 입력 형식으로 사용하지만 설치 엔진은 pip가 아니라 uv다.
- `default` 커널의 패키지를 상속하거나 공유하지 않는다.

### 18~25행: 3102311 환경 생성과 설치

```dockerfile
RUN uv venv --no-project --clear \
        --python /usr/local/bin/python3.10 /opt/venvs/3102311 \
    && uv pip install --strict --python /opt/venvs/3102311/bin/python \
        "ipykernel>=6.30,<7" \
    && if [ -s /opt/jupyter-env/3102311/requirements.txt ]; then \
        uv pip install --strict --python /opt/venvs/3102311/bin/python \
            --requirements /opt/jupyter-env/3102311/requirements.txt; \
    fi
```

- `uv venv`: `/opt/venvs/3102311` 가상환경을 만든다.
- `--no-project`: 주변 `pyproject.toml`이나 uv 프로젝트 탐색의 영향을 받지 않는다.
- `--clear`: 같은 경로가 있으면 기존 내용을 비우고 다시 만든다.
- `--python`: 정확히 `/usr/local/bin/python3.10`을 사용한다.
- 첫 `uv pip install`: 커널 프로세스에 필수인 `ipykernel`을 설치한다.
- `--strict`: 설치 후 환경의 의존성 불일치를 검사해 경고로 드러낸다.
- `--python`: 셸 activate에 의존하지 않고 설치 대상 환경을 명시한다.
- `if [ -s ... ]`: requirements가 비어 있지 않을 때만 사용자 패키지를 설치한다.
- `--requirements`: 기존 requirements.txt 형식을 그대로 읽는다.
- 여러 명령을 `&&`로 연결해 앞 단계 실패 시 이미지 빌드를 즉시 중단한다.

## 4. 최종 Python 3.11 이미지

### 27행: 최종 베이스 이미지

```dockerfile
FROM python:3.11-slim-bullseye
```

- 실제 배포되는 최종 이미지다.
- Jupyter 서버와 `default` 커널은 Python 3.11을 사용한다.
- Python 3.10.11 스테이지와 동일한 bullseye ABI를 사용한다.

### 29행: 최종 스테이지의 인덱스 재선언

```dockerfile
ARG UV_DEFAULT_INDEX
```

- 최종 스테이지의 `uv pip install`도 같은 Nexus를 사용하게 한다.
- 런타임 ENV로 남기지 않는 목적은 8행과 같다.

### 31행: 최종 이미지에 uv 복사

```dockerfile
COPY --from=uv /uv /uvx /bin/
```

- Jupyter 서버, default 커널, Executor 확장을 uv로 설치하기 위해 uv를 다시 복사한다.
- 각 스테이지는 독립된 파일시스템이므로 10행의 복사와 별개로 필요하다.
- uv 바이너리는 최종 이미지에도 남지만 런타임에서 자동으로 패키지를 변경하지 않는다.

### 33~38행: 배포 기본값

```dockerfile
ENV JUPYTER_ROOT_DIR=/workspace/jupyter \
    JUPYTER_TOKEN=default
```

- `JUPYTER_ROOT_DIR`: Jupyter Contents API 루트이자 노트북·아티팩트 작업공간이다.
- `JUPYTER_TOKEN`: Jupyter REST/WebSocket 인증 토큰이다.
- Kubernetes ConfigMap과 Secret 환경변수로 각각 덮어쓸 수 있다.
- 기본 토큰 `default`는 테스트 편의를 위한 값이므로 운영에서는 반드시 교체한다.
- 런타임에 루트를 변경하면 해당 경로가 존재하고 UID/GID 1000이 쓸 수 있어야 한다.

### 40~47행: 공통 런타임·uv 환경

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

- `DEBIAN_FRONTEND=noninteractive`: apt가 이미지 빌드 중 질문을 기다리지 않게 한다.
- `PATH`: 서버 가상환경의 Jupyter 실행 파일을 우선 사용한다.
- `HOME`: 비-root `jovyan` 사용자의 홈을 명시한다.
- `JUPYTER_CONFIG_DIR`: 이미지 설정 파일을 찾는 위치다.
- `JUPYTER_PATH`: kernelspec과 Jupyter data 파일을 찾는 기준 경로다.
- 세 `UV_*` 값은 3.10 스테이지와 같은 설치 정책을 최종 스테이지에 적용한다.
- uv는 런타임 요청을 처리하는 데 사용되지 않으므로 Executor 실행 결과에는 영향을
  주지 않는다.

### 49~69행: OS 런타임 패키지

```dockerfile
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ... \
    && rm -rf /var/lib/apt/lists/*
```

- `apt-get update`: apt 패키지 인덱스를 갱신한다. 폐쇄망에서는 사내 Debian 미러가
  필요하다.
- `--yes`: 설치 질문에 자동 동의한다.
- `--no-install-recommends`: 필수 의존성만 설치한다.
- 마지막 `rm`은 설치 패키지가 아니라 apt 인덱스 캐시만 제거한다.

| 패키지 | 목적 |
|---|---|
| `ca-certificates` | Nexus 및 HTTPS API 인증서 검증 |
| `curl` | 컨테이너 내부 REST 진단과 로컬 Compose 헬스체크 |
| `fonts-dejavu-core` | matplotlib 이미지의 기본 글꼴 |
| `libbz2-1.0` | Python `bz2` 압축 모듈 |
| `libdb5.3` | Python/시스템 DBM 기능 |
| `libexpat1` | XML 파싱 런타임 |
| `libgdbm6` | Python `dbm.gnu` 런타임 |
| `libgomp1` | NumPy·SciPy 등의 OpenMP 병렬 연산 |
| `libgssapi-krb5-2` | Kerberos/GSSAPI 연동 런타임 |
| `liblzma5` | Python `lzma`/xz 압축 |
| `libncursesw6` | 터미널 및 대화형 Python 기능 |
| `libnsl2` | 네트워크 관련 네이티브 모듈 호환성 |
| `libreadline8` | Python 대화형 입력 기능 |
| `libsqlite3-0` | Python `sqlite3`와 Jupyter의 SQLite 사용 |
| `libtirpc3` | RPC/네트워크 네이티브 의존성 |
| `libuuid1` | 시스템 UUID 런타임 |
| `tini` | PID 1, 종료 신호 전달, 좀비 프로세스 회수 |
| `zlib1g` | gzip/zip 압축 런타임 |

## 5. Python 3.10.11 환경 결합

### 71~74행: 인터프리터·표준 라이브러리·가상환경 복사

```dockerfile
COPY --from=python310 /usr/local/bin/python3.10 /usr/local/bin/python3.10
COPY --from=python310 /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.10.so.1.0
COPY --from=python310 /usr/local/lib/python3.10 /usr/local/lib/python3.10
COPY --from=python310 /opt/venvs/3102311 /opt/venvs/3102311
```

- Python 실행 파일, 공유 라이브러리, 표준 라이브러리를 함께 가져온다.
- uv로 만든 `3102311` 환경도 동일한 절대 경로로 복사한다.
- 두 스테이지가 같은 bullseye 기반이라 별도 OpenSSL·libffi 파일 복사가 없다.
- 일부만 제거하면 인터프리터 시작 또는 표준 모듈 import가 실패할 수 있다.

## 6. Jupyter 서버와 default 환경

### 76~77행: 패키지 목록 복사

```dockerfile
COPY environments/server/requirements.txt /opt/jupyter-env/server/requirements.txt
COPY environments/default/requirements.txt /opt/jupyter-env/default/requirements.txt
```

- 서버와 분석 커널의 의존성을 분리한다.
- 서버 목록에는 JupyterLab/Jupyter Server 같은 서비스 패키지를 둔다.
- default 목록에는 사용자가 import할 분석 패키지를 둔다.
- 목록이 달라질 때 Docker 패키지 설치 레이어만 다시 빌드된다.

### 79~89행: Python 3.11 환경 생성과 설치

```dockerfile
RUN ldconfig \
    && uv venv --no-project --clear \
        --python /usr/local/bin/python3.11 /opt/venvs/jupyter \
    && uv venv --no-project --clear \
        --python /usr/local/bin/python3.11 /opt/venvs/default \
    && uv pip install --strict --python /opt/venvs/jupyter/bin/python \
        --requirements /opt/jupyter-env/server/requirements.txt \
    && uv pip install --strict --python /opt/venvs/default/bin/python \
        "ipykernel>=6.30,<7" \
    && uv pip install --strict --python /opt/venvs/default/bin/python \
        --requirements /opt/jupyter-env/default/requirements.txt
```

- `ldconfig`: 복사한 `libpython3.10.so.1.0`을 동적 링커 캐시에 반영한다.
- 첫 `uv venv`: Jupyter 서버 전용 환경을 만든다.
- 두 번째 `uv venv`: default 분석 커널 환경을 독립적으로 만든다.
- 각 `uv pip install --python`은 대상 환경을 명시하므로 셸 activate가 필요 없다.
- default 환경에는 커널 구동을 위해 `ipykernel`을 별도로 설치한다.
- 두 환경을 분리하면 분석 패키지 변경이 서버 의존성과 충돌할 위험이 줄어든다.

requirements의 현재 버전 범위는 빌드 때 해석된다. uv 사용만으로 완전한 재현성이
생기는 것은 아니다. 동일 버전 재빌드가 필요하면 별도의 lock 또는 constraints
생성·검증 정책을 도입해야 한다.

## 7. Executor 확장과 kernelspec

### 91~93행: 확장 소스와 활성화 설정

```dockerfile
COPY extension /opt/jupyter-resource-extension
COPY executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json
```

- `extension/`은 Executor용 작업공간, 노트북, 파일, 자원 API를 추가한다.
- JSON 파일은 Jupyter Server가 해당 확장을 자동 활성화하게 한다.
- 소스 복사, 패키지 설치, 활성화 설정이 모두 필요하다.

### 95~105행: 확장 설치와 커널 등록

```dockerfile
RUN uv pip install --strict --python /opt/venvs/jupyter/bin/python \
        --no-deps /opt/jupyter-resource-extension \
    && /opt/venvs/default/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=default \
        --display-name="Default (Python 3.11)" \
    && /opt/venvs/3102311/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=3102311 \
        --display-name="3102311 (Python 3.10.11)" \
    && rm -rf /opt/venvs/jupyter/share/jupyter/kernels/python3
```

- 확장은 서버 Python에 uv로 설치한다.
- `--no-deps`: 확장 의존성은 서버 requirements가 소유하므로 임의 변경을 막는다.
- `ipykernel install --prefix`: 두 kernelspec을 서버가 검색하는 경로에 등록한다.
- `--name`: Executor와 API가 사용하는 안정적인 식별자다.
- `--display-name`: JupyterLab UI에 표시되는 이름이다.
- 자동 생성된 `python3` kernelspec 디렉터리는 허용한 두 커널 외 선택지를 줄이기 위해
  제거한다. 실제 허용 정책은 `jupyter_server_config.py`도 함께 강제한다.

## 8. 사용자와 PVC 권한

### 107~116행: UID/GID 1000 사용자 생성

```dockerfile
RUN rm -rf /home/jovyan \
    && groupadd --gid 1000 jovyan \
    && useradd \
        --uid 1000 \
        --gid 1000 \
        --create-home \
        --shell /bin/bash \
        jovyan \
    && mkdir -p "${JUPYTER_ROOT_DIR}" /home/jovyan/.jupyter \
    && chown -R 1000:1000 "${JUPYTER_ROOT_DIR}" /home/jovyan
```

- 이 단계는 root로 수행되어 사용자·그룹과 디렉터리를 만든다.
- 고정 UID/GID는 Kubernetes PVC의 숫자 기반 권한 판정과 맞추기 위한 것이다.
- `--create-home`은 `/home/jovyan`을 만든다.
- 작업공간과 설정 디렉터리를 미리 만들고 UID/GID 1000에 소유권을 준다.
- 비-root 전환 후 JupyterLab과 커널이 파일을 생성·수정할 수 있게 한다.

### 이미지 권한과 PVC 권한의 차이

빌드 중 `chown`은 이미지 내부 기본 디렉터리에만 적용된다. Kubernetes가 같은 경로에
PVC를 마운트하면 이미지 디렉터리는 가려지고 PVC 자체 소유권과 모드가 적용된다.

- PVC를 UID/GID 1000이 읽고 쓸 수 있어야 한다.
- 스토리지가 지원하면 Pod `securityContext.fsGroup: 1000`을 사용한다.
- 지원하지 않으면 승인된 initContainer 또는 스토리지 측에서 권한을 준비한다.
- `JUPYTER_ROOT_DIR`을 바꾸면 새 경로에도 같은 권한 조건이 필요하다.
- nbviewer처럼 다른 UID가 읽는 경우 파일 `0644`와 모든 상위 디렉터리의 탐색 권한이
  필요하다.

### 118~120행: 설정과 시작 스크립트

```dockerfile
COPY --chown=1000:1000 jupyter_server_config.py \
    /home/jovyan/.jupyter/jupyter_server_config.py
COPY --chmod=755 start-jupyter.sh /usr/local/bin/start-jupyter
```

- 설정 파일을 Jupyter 사용자 홈에 복사한다.
- HOME 전체를 빈 볼륨으로 덮으면 이 파일이 가려질 수 있으므로 주의한다.
- 시작 스크립트를 `0755`로 고정해 Windows Git 체크아웃의 실행 비트 차이를 제거한다.

### 122행: 비-root 실행

```dockerfile
USER 1000:1000
```

- 이후 컨테이너 프로세스는 root가 아니라 `jovyan` 권한으로 실행된다.
- JupyterLab과 사용자 코드가 시스템 경로를 임의 수정하는 위험을 줄인다.
- 추가 OS 패키지는 이 줄 이전의 이미지 빌드 단계에서 설치해야 한다.

### 123행: 기본 작업 디렉터리

```dockerfile
WORKDIR "${JUPYTER_ROOT_DIR}"
```

- 컨테이너 프로세스의 기본 시작 위치다.
- 이 값은 이미지 빌드 때 확정된다. 런타임 환경변수만 변경해도 Docker working
  directory는 자동으로 바뀌지 않는다.
- Jupyter Contents 루트는 서버 설정이 런타임 환경변수를 읽으므로 변경된 값을 쓴다.
- 엄격한 일치가 필요하면 Kubernetes Deployment의 `workingDir`도 맞춘다.

## 9. 컨테이너 시작

### 125행: 포트 메타데이터

```dockerfile
EXPOSE 8888
```

- 이미지가 8888 포트를 사용함을 문서화한다.
- 실제 Service나 호스트 포트 공개는 별도 배포 설정이다.

### 127행: tini와 시작 스크립트

```dockerfile
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-jupyter"]
```

- `tini`가 PID 1로서 종료 신호 전달과 좀비 프로세스 회수를 담당한다.
- 장시간 커널 취소와 Pod 종료 신뢰성을 위해 유지한다.
- 시작 스크립트는 토큰·루트 값을 검증한 뒤 `exec jupyter lab "$@"`를 실행한다.
- Kubernetes `args`는 JupyterLab 인자로 전달된다.

## 10. 폐쇄망 빌드

```shell
docker build \
  --build-arg UV_IMAGE=harbor.example.com/library/uv:0.12.8 \
  --build-arg UV_DEFAULT_INDEX=https://nexus.example.com/repository/pypi-group/simple/ \
  -t ${image.tag} .
```

- Python 3.10.11·3.11 base image와 uv 0.12.8 이미지를 Harbor에 미러링한다.
- Dockerfile의 Python `FROM` 주소도 조직의 이미지 참조 규칙에 맞춘다.
- apt는 uv/Nexus와 무관하므로 별도의 Debian bullseye 미러가 필요하다.
- Nexus에는 모든 직접·전이 의존성과 대상 Linux 아키텍처 wheel 또는 빌드 가능한
  source distribution이 있어야 한다.
- 사내 CA는 이미지의 시스템 CA 신뢰 저장소에 조직 표준 방식으로 추가한다.
- 비밀값을 Dockerfile, requirements 또는 build argument에 넣지 않는다.

## 11. 변경 시 함께 확인할 항목

| 변경 | 함께 확인할 대상 |
|---|---|
| uv 버전/이미지 | Harbor 미러, 이미지 digest, 두 스테이지 빌드 |
| Nexus 주소 | `UV_DEFAULT_INDEX`, 익명 접근 또는 CI의 승인된 인증 방식 |
| Python 버전 | 베이스 이미지, venv 경로, kernelspec, OS ABI, 테스트 |
| 커널 이름 | Jupyter 설정, Executor profile, smoke/e2e 스크립트 |
| UID/GID | PVC 소유권, Pod securityContext, nbviewer 접근 |
| 작업공간 루트 | PVC mountPath, 환경변수, 필요 시 workingDir |
| requirements | Nexus 패키지 완전성, 커널별 독립성, lock 정책 |
| 확장 코드 | 확장 테스트, Executor-Jupyter 계약 테스트, 이미지 재빌드 |

## 12. 테스트 하네스 Dockerfile과의 차이

[`../../test_harness/jupyter/Dockerfile`](../../test_harness/jupyter/Dockerfile)은 같은 핵심
이미지 구조를 로컬에서 검증한다.

- `COPY` 원본에 `test_harness/jupyter/` 접두사가 붙는다.
- 작업공간 기본값은 `/workspace/pv`다.
- 기본 `UV_DEFAULT_INDEX`는 공개 PyPI이며 빌드 인자로 Nexus로 변경할 수 있다.
- 배포용 기본 토큰을 이미지에 두지 않고 실행 환경에서 주입한다.
- Python 버전, uv 버전, 가상환경 생성, 패키지 설치, 커널 등록, 권한 구조는 배포용과
  동일하다.

실제 운영 전달 문서는 `deploy/jupyter`를 기준으로 하고 테스트 하네스는 Executor
개발·검증에 사용한다.
