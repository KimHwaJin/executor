# Jupyter Dockerfile 상세 가이드

이 문서는 [`Dockerfile`](Dockerfile)을 구문 단위로 설명한다. 단순히 Docker 문법을
풀이하는 것이 아니라, 현재 Executor용 Jupyter 이미지에서 각 구문이 필요한 이유와
변경·삭제할 때의 영향까지 기록한다.

기준 구조는 다음과 같다.

- Jupyter 서버: Python 3.11, `/opt/venvs/jupyter`
- `default` 커널: Python 3.11, `/opt/venvs/default`
- `3102311` 커널: 정확히 Python 3.10.11, `/opt/venvs/3102311`
- 컨테이너 실행 사용자: `jovyan`, UID/GID `1000:1000`
- 작업공간 기본 경로: `/workspace/jupyter`

> 줄 번호는 현재 Dockerfile을 이해하기 위한 보조 정보다. Dockerfile이 수정되면 줄
> 번호가 달라질 수 있으므로 실제 구문도 함께 확인한다.

## 1. 전체 빌드 구조

이 Dockerfile은 다단계 빌드(multi-stage build)를 사용한다.

1. `python310` 스테이지에서 Python 3.10.11 커널 환경을 만든다.
2. 최종 Python 3.11 이미지로 3.10.11 인터프리터와 가상환경을 복사한다.
3. 최종 이미지에서 Jupyter 서버와 `default` 커널 환경을 만든다.
4. 두 커널을 Jupyter 서버에 등록한다.
5. root가 아닌 UID/GID 1000 사용자로 JupyterLab을 실행한다.

Python 3.10.11과 3.11 모두 Debian 11 bullseye 기반 공식 slim 이미지를 사용한다.
동일한 OS ABI 위에서 두 Python 버전을 제공하므로 OpenSSL·libffi 호환 파일을 따로
복사하지 않는다.

> Debian 11 bullseye는 2026년 8월 31일 LTS가 종료되었다. 이 프로젝트는 두 Python
> 환경의 ABI 통일을 우선해 bullseye 사용을 결정했다. 운영에서는 조직의 이미지 스캔,
> 보완 패치 및 Extended LTS 정책을 별도로 적용해야 한다.

## 2. Python 3.10.11 빌드 스테이지

### 1행: Python 3.10.11 전용 스테이지

```dockerfile
FROM python:3.10.11-slim-bullseye AS python310
```

- 정확히 Python 3.10.11이 포함된 공식 이미지를 사용한다.
- `AS python310`은 이 스테이지에 이름을 붙인다. 최종 스테이지가
  `COPY --from=python310`으로 파일을 가져올 때 사용한다.
- 이 스테이지 자체는 최종 컨테이너가 아니다. 커널 환경을 만드는 빌드 재료다.
- `3.10`이나 `latest`처럼 느슨한 태그로 바꾸면 패치 버전 3.10.11 보장이 깨진다.

### 3행: pip 버전 확인 비활성화

```dockerfile
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
```

- pip가 실행될 때마다 새 버전 확인 메시지를 출력하지 않게 한다.
- 빌드 로그의 불필요한 네트워크 확인과 안내 메시지를 줄인다.
- 패키지 설치 기능 자체를 끄거나 버전을 고정하는 설정은 아니다.

### 5행: 3102311 패키지 목록 복사

```dockerfile
COPY environments/3102311/requirements.txt /opt/jupyter-env/3102311/requirements.txt
```

- 배포 담당자가 제공받은 3.10.11 전용 패키지 목록을 이미지에 넣는다.
- 현재 파일은 의도적으로 비어 있으며, 승인된 목록을 그대로 붙여넣는 자리다.
- `default` 커널 패키지를 상속하지 않는다. 두 커널은 완전히 독립적이다.
- requirements가 바뀌면 이 줄 이후의 Docker 빌드 캐시만 무효화된다.

### 6행: 사내 Python 패키지 저장소 설정

```dockerfile
COPY pip.conf /etc/pip.conf
```

- 이 스테이지의 pip가 사용할 전역 설정을 넣는다.
- 폐쇄망에서는 `pip.conf`의 예시 URL을 실제 Nexus PyPI 주소로 바꿔야 한다.
- 계정·비밀번호나 토큰을 파일에 작성하면 이미지 레이어에 남으므로 금지한다.
- 3.10.11 스테이지와 최종 스테이지는 서로 다른 파일시스템이므로 68행에서 한 번 더
  복사해야 한다.

### 8~13행: 3.10.11 가상환경 준비

```dockerfile
RUN python3.10 -m venv --copies /opt/venvs/3102311 \
    && /opt/venvs/3102311/bin/pip install --no-cache-dir "ipykernel>=6.30,<7" \
    && if [ -s /opt/jupyter-env/3102311/requirements.txt ]; then \
        /opt/venvs/3102311/bin/pip install --no-cache-dir \
            -r /opt/jupyter-env/3102311/requirements.txt; \
    fi
```

- `python3.10 -m venv --copies`: `/opt/venvs/3102311`을 만든다.
  - `--copies`는 실행 파일을 심볼릭 링크 대신 복사한다.
  - 이 가상환경을 다른 베이스 이미지로 옮기므로 원본 스테이지 경로를 가리키는
    심볼릭 링크가 남지 않게 하는 것이 중요하다.
- `ipykernel` 설치: 이 Python 환경을 Jupyter 커널로 구동하는 필수 런타임이다.
  분석 패키지 목록과 독립적으로 항상 설치한다.
- `--no-cache-dir`: pip 다운로드 캐시를 이미지 레이어에 남기지 않아 이미지 크기를
  줄인다. 설치된 패키지는 그대로 유지된다.
- `if [ -s ... ]`: requirements 파일의 크기가 0보다 클 때만 설치한다. 지금처럼 빈
  파일이어도 빌드가 정상 진행된다.
- 최종 Python 3.11 이미지도 bullseye 기반이므로 3.10.11 전용 OpenSSL·libffi 호환
  라이브러리를 별도로 복사할 필요가 없다.
- 여러 명령을 하나의 `RUN`으로 묶은 이유는 중간 레이어를 줄이고 앞 단계가 실패하면
  불완전한 환경이 다음 단계에 남지 않게 하기 위해서다.

## 3. 최종 Python 3.11 이미지와 런타임 환경

### 15행: 최종 이미지 시작점

```dockerfile
FROM python:3.11-slim-bullseye
```

- 실제 배포되는 최종 이미지의 기반이다.
- Jupyter 서버와 `default` 커널은 Python 3.11을 사용한다.
- Python 3.10.11 스테이지와 같은 Debian 11 세대를 사용해 시스템 라이브러리 ABI를
  통일한다.
- 앞의 `python310` 스테이지에 설치된 불필요한 빌드 흔적은 자동으로 제외되고, 이후
  명시적으로 복사한 파일만 최종 이미지에 들어온다.

### 17~22행: 배포 기본값

```dockerfile
ENV JUPYTER_ROOT_DIR=/workspace/jupyter \
    JUPYTER_TOKEN=default
```

- `JUPYTER_ROOT_DIR`: Jupyter Contents API의 루트이자 노트북·아티팩트가 보이는
  작업공간 기본 경로다.
- `JUPYTER_TOKEN`: REST/WebSocket 인증 토큰의 이미지 기본값이다.
- 두 값은 Kubernetes의 ConfigMap/Secret 환경변수로 덮어쓸 수 있다.
- `default` 토큰은 로컬 확인용이다. 운영에서는 반드시 Secret으로 교체한다.
- 루트 경로를 런타임에 바꾸는 경우 해당 경로가 실제로 존재하고 UID/GID 1000이 쓸 수
  있어야 한다. Docker 빌드 중의 `mkdir/chown`은 기본 경로에만 적용된다.

### 24~29행: 공통 프로세스 환경

```dockerfile
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venvs/jupyter/bin:${PATH}" \
    HOME=/home/jovyan \
    JUPYTER_CONFIG_DIR=/home/jovyan/.jupyter \
    JUPYTER_PATH=/opt/venvs/jupyter/share/jupyter
```

- `DEBIAN_FRONTEND=noninteractive`: apt가 빌드 도중 대화형 질문을 기다리지 않게 한다.
- `PIP_DISABLE_PIP_VERSION_CHECK=1`: 최종 스테이지의 pip에도 버전 확인을 끈다.
- `PATH`: 별도 경로를 쓰지 않고 `jupyter`, `jupyter-lab`을 호출해도 서버 전용
  가상환경 실행 파일이 우선 선택된다.
- `HOME`: 비-root 실행 사용자의 홈을 명시한다. Jupyter 런타임 파일과 사용자 설정이
  root 홈으로 흘러가지 않게 한다.
- `JUPYTER_CONFIG_DIR`: 이미지에 넣은 `jupyter_server_config.py`를 찾을 위치다.
- `JUPYTER_PATH`: 서버가 kernelspec과 Jupyter data 파일을 찾는 기준 경로다.

### 31~51행: OS 런타임 패키지 설치

```dockerfile
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ... \
    && rm -rf /var/lib/apt/lists/*
```

- `apt-get update`: 현재 apt 저장소의 패키지 인덱스를 받는다. 폐쇄망에서는 Docker
  빌드가 접근 가능한 사내 Debian 미러 설정이 필요하다.
- `--yes`: 설치 확인 질문에 자동 동의한다.
- `--no-install-recommends`: 필수 의존성만 설치해 이미지 크기와 공격 표면을 줄인다.
- 각 패키지의 목적은 다음과 같다.

| 패키지 | 목적 |
|---|---|
| `ca-certificates` | HTTPS 인증서 검증. Nexus 및 HTTPS API 접근에 필요 |
| `curl` | 운영 진단이나 컨테이너 내부 연결 확인용 HTTP 클라이언트 |
| `fonts-dejavu-core` | matplotlib 등에서 기본 글꼴 없이 이미지 생성이 깨지는 문제 방지 |
| `libbz2-1.0` | Python `bz2` 압축 모듈 런타임 |
| `libdb5.3` | 일부 Python/시스템 DBM 기능의 런타임 |
| `libexpat1` | XML 파싱 런타임 |
| `libgdbm6` | Python `dbm.gnu` 런타임 |
| `libgomp1` | NumPy·SciPy·ML 패키지의 OpenMP 병렬 연산 런타임 |
| `libgssapi-krb5-2` | Kerberos/GSSAPI 연동 패키지의 런타임 |
| `liblzma5` | Python `lzma`/xz 압축 런타임 |
| `libncursesw6` | 터미널 및 대화형 Python 기능 런타임 |
| `libnsl2` | 일부 네트워크 관련 네이티브 모듈 호환성 |
| `libreadline8` | Python 대화형 입력 및 히스토리 기능 |
| `libsqlite3-0` | Python `sqlite3`, Jupyter 내부 SQLite 사용 가능성 지원 |
| `libtirpc3` | 일부 RPC/네트워크 네이티브 의존성 |
| `libuuid1` | UUID 관련 시스템 런타임 |
| `tini` | PID 1, 종료 신호 전달, 좀비 프로세스 회수 |
| `zlib1g` | gzip/zip 및 여러 Python 패키지의 압축 런타임 |

- 마지막 `rm -rf /var/lib/apt/lists/*`는 설치된 패키지가 아니라 apt 인덱스 캐시만
  삭제하여 이미지 크기를 줄인다.

## 4. Python 환경 조립

### 53~56행: 3.10.11 런타임을 최종 이미지로 복사

```dockerfile
COPY --from=python310 /usr/local/bin/python3.10 /usr/local/bin/python3.10
COPY --from=python310 /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.10.so.1.0
COPY --from=python310 /usr/local/lib/python3.10 /usr/local/lib/python3.10
COPY --from=python310 /opt/venvs/3102311 /opt/venvs/3102311
```

- Python 실행 파일, 공유 라이브러리, 표준 라이브러리를 각각 복사한다.
- `3102311` 가상환경도 함께 가져온다.
- 하나라도 빠지면 인터프리터 시작, 표준 모듈 import 또는 네이티브 라이브러리 로딩이
  실패할 수 있으므로 한 묶음으로 관리한다.

### 58~60행: 서버·default 패키지 목록과 pip 설정 복사

```dockerfile
COPY environments/server/requirements.txt /opt/jupyter-env/server/requirements.txt
COPY environments/default/requirements.txt /opt/jupyter-env/default/requirements.txt
COPY pip.conf /etc/pip.conf
```

- 서버 환경과 분석 커널 환경의 의존성을 분리한다.
- 서버 requirements에는 JupyterLab/Jupyter Server 같은 서비스 패키지만 둔다.
- default requirements에는 사용자가 코드에서 import할 분석 패키지만 둔다.
- 최종 스테이지도 독립된 파일시스템이므로 `pip.conf`를 다시 복사한다.

### 62~70행: 서버 및 default 가상환경 생성

```dockerfile
RUN ldconfig \
    && /usr/local/bin/python3.11 -m venv /opt/venvs/jupyter \
    && /usr/local/bin/python3.11 -m venv /opt/venvs/default \
    && /opt/venvs/jupyter/bin/pip install --no-cache-dir \
        -r /opt/jupyter-env/server/requirements.txt \
    && /opt/venvs/default/bin/pip install --no-cache-dir \
        "ipykernel>=6.30,<7" \
    && /opt/venvs/default/bin/pip install --no-cache-dir \
        -r /opt/jupyter-env/default/requirements.txt
```

- `ldconfig`: `/usr/local/lib`로 복사한 `libpython3.10.so.1.0`을 동적 링커 캐시에
  반영한다.
- `/opt/venvs/jupyter`: Jupyter 서버 프로세스 전용 환경이다.
- `/opt/venvs/default`: 사용자 분석 코드 전용 Python 3.11 환경이다.
- 두 환경을 나누면 분석 패키지 변경이 Jupyter 서버 의존성과 충돌하는 위험이 줄어든다.
- default 환경에도 커널 구동을 위해 `ipykernel`을 항상 설치한다.

## 5. Executor 확장과 kernelspec 등록

### 72~74행: 확장 소스 및 활성화 설정 복사

```dockerfile
COPY extension /opt/jupyter-resource-extension
COPY executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json
```

- `extension/`은 Executor가 사용하는 작업공간 준비, 노트북 반영, 파일 및 자원 관련
  엔드포인트를 Jupyter Server에 추가한다.
- JSON 파일은 해당 서버 확장을 자동 활성화한다.
- 코드를 복사하는 것만으로는 활성화되지 않으므로 설치와 활성화 설정이 모두 필요하다.

### 76~86행: 확장 설치와 커널 등록

```dockerfile
RUN /opt/venvs/jupyter/bin/pip install --no-cache-dir --no-deps \
        /opt/jupyter-resource-extension \
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

- 확장은 Jupyter 서버 환경에 설치한다. 커널 환경에 설치하지 않는다.
- `--no-deps`: 확장의 의존성은 서버 requirements가 소유한다. 설치 과정에서 임의로
  외부 패키지를 추가하거나 서버 패키지 버전을 바꾸지 않게 한다.
- `ipykernel install --prefix=/opt/venvs/jupyter`: 두 커널의 kernelspec을 Jupyter 서버가
  검색하는 경로에 등록한다.
- `--name`: API 및 Executor가 사용하는 안정적인 식별자다.
- `--display-name`: JupyterLab UI에서 사용자에게 보이는 이름이다.
- 자동 생성된 `python3` kernelspec은 정책상 허용한 두 커널 외의 선택지가 노출되지
  않게 삭제한다.
- 이 부분의 이름을 바꾸면 Executor runtime profile, 테스트, Jupyter 설정의
  `allowed_kernelspecs`도 함께 바꿔야 한다.

## 6. 사용자와 파일 권한

### 88~97행: UID/GID 1000 사용자 생성

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

- 빌드 중인 이 시점의 사용자는 root다. 사용자·그룹 생성과 소유권 변경에는 root
  권한이 필요하다.
- `rm -rf /home/jovyan`: 같은 경로가 이전 레이어에 존재하더라도 새 UID/GID의 홈을
  일관되게 만들기 위한 초기화다. PVC 데이터나 호스트 홈을 삭제하는 명령이 아니다.
- `groupadd --gid 1000 jovyan`: 고정 GID 1000 그룹을 만든다.
- `useradd --uid 1000 --gid 1000`: 고정 UID/GID 사용자를 만든다.
  - Kubernetes PVC가 숫자 UID/GID 기준으로 권한을 판정하므로 이름보다 숫자가 중요하다.
  - 여러 Jupyter Pod와 nbviewer가 같은 PVC를 읽을 때도 숫자 권한이 기준이다.
- `--create-home`: `/home/jovyan`과 기본 사용자 파일을 만든다.
- `--shell /bin/bash`: 컨테이너 디버깅 및 Jupyter 터미널의 기본 셸을 지정한다.
- `mkdir -p`: 기본 작업공간과 Jupyter 설정 디렉터리를 미리 만든다.
- `chown -R 1000:1000`: 컨테이너가 비-root 사용자로 전환된 뒤 해당 위치에 쓰게 한다.

### PVC 권한에서 가장 중요한 점

이미지 빌드 중 `chown`은 **이미지 내부 디렉터리**에만 적용된다. Kubernetes에서 같은
경로에 PVC를 마운트하면 이미지 레이어의 디렉터리는 가려지고 PVC 자체의 소유권과
모드가 적용된다. 따라서 다음 조건은 배포에서 별도로 충족해야 한다.

- PVC 루트를 UID/GID 1000이 읽고 쓸 수 있어야 한다.
- 스토리지 드라이버가 지원하면 Pod `securityContext.fsGroup: 1000`을 사용한다.
- `fsGroup`이 적용되지 않는 스토리지라면 승인된 initContainer 또는 스토리지 측
  권한 설정으로 준비한다.
- `JUPYTER_ROOT_DIR`을 기본값과 다른 PVC 마운트 경로로 덮어쓰면 그 경로 역시 동일한
  권한 조건을 만족해야 한다.
- 노트북을 nbviewer의 UID 65534가 읽어야 한다면 파일 모드 `0644`와 상위 디렉터리의
  탐색 권한이 필요하다. 파일만 `0644`여도 상위 디렉터리에 `x` 권한이 없으면 읽지
  못한다.

### 99~101행: 설정과 시작 스크립트 복사

```dockerfile
COPY --chown=1000:1000 jupyter_server_config.py \
    /home/jovyan/.jupyter/jupyter_server_config.py
COPY --chmod=755 start-jupyter.sh /usr/local/bin/start-jupyter
```

- Jupyter 설정 파일은 실행 사용자 소유로 복사한다. HOME 전체를 빈 볼륨으로
  덮어쓰면 이 파일이 가려지므로 주의한다.
- 시작 스크립트는 `0755`로 복사한다.
  - 소유자는 읽기·쓰기·실행 가능하다.
  - 그룹과 다른 사용자는 읽기·실행 가능하다.
  - 스크립트는 수정할 필요가 없고 실행만 가능하면 되므로 root 소유여도 문제없다.
- `COPY --chmod`는 저장소 체크아웃 환경의 실행 비트 차이, 특히 Windows Git에서 생길
  수 있는 차이를 이미지 안에서 정규화한다.

### 103행: 비-root 사용자 전환

```dockerfile
USER 1000:1000
```

- 이후 빌드 명령과 컨테이너 실행 프로세스의 기본 사용자를 고정한다.
- JupyterLab 및 커널이 root로 실행되지 않아 권한 오용과 보안 위험을 줄인다.
- 실행 중 apt 설치, 임의의 시스템 경로 수정은 불가능해진다. 필요한 OS 패키지는
  반드시 이 줄보다 앞에서 이미지에 포함해야 한다.

### 104행: 기본 작업 디렉터리

```dockerfile
WORKDIR "${JUPYTER_ROOT_DIR}"
```

- 컨테이너 프로세스의 시작 디렉터리를 빌드 시 기본 루트로 정한다.
- `WORKDIR` 값은 이미지 빌드 때 확정된다. 런타임에 `JUPYTER_ROOT_DIR` 환경변수만 다른
  값으로 덮어써도 Docker의 시작 디렉터리 자체는 자동 변경되지 않는다.
- 다만 Jupyter의 실제 Contents 루트는 `jupyter_server_config.py`가 런타임 환경변수를
  읽어 설정하므로, 유효하고 쓰기 가능한 경로라면 서비스 기능은 변경된 루트를 쓴다.
- Kubernetes에서 엄격한 일치를 원하면 Deployment의 `workingDir`도 PVC 마운트 경로로
  설정한다.

## 7. 컨테이너 시작

### 106행: 포트 메타데이터

```dockerfile
EXPOSE 8888
```

- 이미지가 8888 포트를 사용할 의도임을 문서화한다.
- 실제로 포트를 외부에 공개하거나 Kubernetes Service를 만들지는 않는다.
- Deployment의 `containerPort`와 Service의 `targetPort`를 별도로 8888에 맞춰야 한다.

### 108행: PID 1과 Jupyter 시작

```dockerfile
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-jupyter"]
```

- `tini`가 컨테이너 PID 1이 되어 SIGTERM 등 종료 신호를 Jupyter와 커널에 전달하고
  종료된 자식 프로세스를 회수한다.
- `--` 뒤는 tini 옵션이 아니라 실제 애플리케이션 명령임을 구분한다.
- JSON exec 형식이라 불필요한 셸을 거치지 않고 신호 전달이 명확하다.
- `start-jupyter`는 토큰과 루트 값이 비었는지 검사한 뒤 `exec jupyter lab "$@"`를
  실행한다. Kubernetes `args`는 이 스크립트를 통해 JupyterLab 인자로 전달된다.
- `tini`를 제거하고 셸을 PID 1로 두면 장시간 작업 취소, Pod 종료, 커널 자식 프로세스
  회수의 신뢰성이 낮아질 수 있다.

## 8. 변경할 때 함께 확인할 항목

| 변경 | 함께 확인할 대상 |
|---|---|
| Python 버전 | 베이스 이미지, venv 경로, kernelspec 이름·표시명, 호환 공유 라이브러리, 테스트 |
| 커널 이름 | `jupyter_server_config.py`, Executor profile 설정, smoke/e2e 스크립트 |
| UID/GID | PVC 소유권, Pod securityContext, nbviewer 접근 권한 |
| 작업공간 루트 | PVC mountPath, `JUPYTER_ROOT_DIR`, 필요 시 `workingDir`, Executor 등록 정보 |
| requirements | Nexus에 모든 직접·전이 의존성과 대상 OS용 wheel이 있는지 확인 |
| Jupyter 포트 | 서버 설정, Deployment containerPort, Service targetPort, Executor endpoint |
| 확장 코드 | 확장 테스트, Executor-Jupyter 계약 테스트, 이미지 재빌드 |

## 9. 빌드·배포 전 권한 체크리스트

1. 컨테이너가 최종적으로 `1000:1000`으로 실행되는가?
2. PVC 마운트 경로를 `1000:1000`이 생성·수정·삭제할 수 있는가?
3. nbviewer 등 읽기 전용 소비자가 파일과 모든 상위 디렉터리를 탐색할 수 있는가?
4. `/home/jovyan`을 볼륨으로 가려 Jupyter 설정 파일을 없애고 있지 않은가?
5. read-only root filesystem을 사용한다면 HOME의 런타임 쓰기 경로와 `/tmp`에 별도
   쓰기 볼륨을 제공했는가?
6. 운영 토큰이 Dockerfile 기본값이 아니라 Secret으로 주입되는가?
7. 각 Jupyter Pod를 고유 Service로 노출하여 커널 REST/WebSocket 요청이 같은 Pod로
   전달되는가?

## 10. 테스트 하네스 Dockerfile과의 차이

[`../../test_harness/jupyter/Dockerfile`](../../test_harness/jupyter/Dockerfile)은 같은 이미지
구조를 검증하기 위한 로컬 테스트용이다. 핵심 Python 환경과 권한 구조는 같지만 다음이
다르다.

- 저장소 루트를 빌드 컨텍스트로 사용하므로 `COPY` 원본 경로에
  `test_harness/jupyter/` 접두사가 붙는다.
- 로컬 테스트 작업공간 기본값은 `/workspace/pv`다.
- 배포 패키지의 `pip.conf`를 복사하지 않으므로 일반 테스트 빌드는 기본 pip 설정을
  따른다.
- 배포용 기본 토큰을 이미지에 두지 않고 실행 환경에서 주입한다.

실제 운영 이미지 설명과 전달 문서는 `deploy/jupyter`를 기준으로 하고, 테스트 하네스는
Executor 개발·검증에만 사용한다.
