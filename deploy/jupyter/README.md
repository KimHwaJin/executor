# Executor용 Jupyter 이미지

이 폴더만으로 JupyterLab, basic·ml 커널, Executor 연동 확장이 포함된 이미지를 빌드한다.
원본 저장소나 테스트 하네스는 필요 없다.

**배포 전 Dockerfile 상단의 `JUPYTER_ROOT_DIR`, `JUPYTER_TOKEN`과
아래 4번 배포 설정을 확인한다. 별도 주입이 없으면 토큰은 `default`로 실행된다.**

## 1. 파일 구성과 수정할 곳

| 파일 | 역할 | 수정이 필요한 경우 |
|---|---|---|
| `Dockerfile` | OS·Python 설치, 가상환경 생성, 커널 등록, 실행 사용자 설정 | 베이스 이미지, apt 미러, OS 패키지, Python 버전, UID/GID 변경 |
| `environments/server/requirements.txt` | Jupyter 서버용 패키지 | JupyterLab·Jupyter Server 버전 변경 |
| `environments/basic/requirements.txt` | basic 커널용 분석 패키지 | 분석 라이브러리 추가·버전 변경 |
| `environments/ml/requirements.txt` | ml 커널용 추가 ML 패키지 | ML 라이브러리 추가·버전 변경 |
| `jupyter_server_config.py` | 루트·토큰 적용, 포트, 허용 커널, 기본 커널 설정 | 포트나 커널 정책 변경. 루트·토큰은 파일 수정 없이 환경변수로 지정 |
| `start-jupyter.sh` | 필수 환경변수 확인 후 JupyterLab 실행 | 일반적으로 수정하지 않음 |
| `executor_resource_extension.json` | Executor 연동 확장 활성화 | 그대로 유지 |
| `extension/pyproject.toml`, `extension/src/` | 자원 조회, 작업공간 준비, 노트북 작성, 파일 다운로드 기능 | Executor 연동 코드이므로 일반 배포 시 수정하지 않음 |
| `.dockerignore` | 비밀 설정·캐시·작업 데이터를 빌드 컨텍스트에서 제외 | 일반적으로 수정하지 않음 |

`ml/requirements.txt`는 `-r ../basic/requirements.txt`로 basic 목록도 설치한다.
공통 분석 패키지는 basic에, ml에서만 쓸 패키지는 ml에 추가한다.
이 상대 디렉토리 구조는 유지해야 한다.

`extension/pyproject.toml`은 커스텀 확장을 pip로 설치하기 위한 필수 파일이다.
uv나 별도 패키징 도구를 실행할 필요는 없다.

## 2. Dockerfile 동작

빌드 시 다음 순서로 구성한다.

1. `python:3.12-slim-bookworm`을 기반으로 Python 3.11, 폰트, 시스템 라이브러리,
   프로세스 종료 신호 처리를 위한 `tini` 등을 apt로 설치한다.
2. 아래 세 가상환경을 만들고 각 `requirements.txt`를 pip로 설치한다.

   | 용도 | Python | 이미지 내부 경로 |
   |---|---|---|
   | JupyterLab 서버 | 3.12 | `/opt/venvs/jupyter` |
   | basic 커널 | 3.11 | `/opt/venvs/basic` |
   | ml 커널 | 3.12 | `/opt/venvs/ml` |

3. 서버 가상환경에 `extension/`을 설치하고 확장을 활성화한다.
4. `basic`, `ml` kernelspec을 서버에 등록하고 불필요한 기본 `python3` kernelspec을 제거한다.
   허용 커널은 basic·ml, 기본 선택은 basic이다.
5. UID/GID `1000:1000` 사용자를 만들고 설정 파일과 시작 스크립트를 복사한다.
6. 해당 사용자로 `/workspace/pv`에서 실행하도록 설정한다.

컨테이너 시작 시 `tini`가 `start-jupyter.sh`를 실행한다. 스크립트는 토큰과 루트 경로가
비어 있지 않은지 확인한 뒤 JupyterLab을 실행한다. 서버 설정 파일은 환경변수를 읽고
`0.0.0.0:8888`에서 요청을 받도록 설정한다.

자원 조회 확장은 서버 프로세스에서 cgroup을 읽는다. basic·ml 커널을 실행해서
측정하는 방식이 아니다.

## 3. 빌드와 이미지 업로드

**이 README와 Dockerfile이 있는 폴더에서** 실행한다. 주소·태그는 실제 값으로 바꾼다.

```shell
docker build -t harbor.example.com/team/executor-jupyter:delivery .
docker login harbor.example.com
docker push harbor.example.com/team/executor-jupyter:delivery
```

빌드 머신과 운영 노드의 아키텍처가 다르면 운영 노드에 맞춰 지정한다.
예를 들어 amd64 노드용은 `docker build --platform linux/amd64 ...`로 빌드한다.

빌드에는 베이스 이미지, apt 저장소, Python 패키지 인덱스 접근이 필요하다.
폐쇄망에서는 사내 이미지·apt 미러와 Nexus/pip 설정을 준비해야 한다.
인증정보는 CI의 비밀 설정으로 제공하고 소스나 이미지 레이어에 넣지 않는다.

## 4. 배포할 때 지정할 값

Dockerfile 상단에 두 설정의 이미지 기본값을 모아 두었다.

```dockerfile
ENV JUPYTER_ROOT_DIR=/workspace/pv \
    JUPYTER_TOKEN=default
```

별도 환경변수 주입 없이도 루트 `/workspace/pv`, 토큰 `default`로 실행된다.
이는 인증 없는 실행이 아니라 `default`를 인증 토큰으로 사용하는 것이다.
공개된 테스트용 기본값이므로 운영 배포 시에는 Secret으로 별도 토큰을 주입한다.
운영 토큰을 Dockerfile에 직접 기록할 필요는 없다. 루트와 토큰 모두 이미지 재빌드 없이
배포 환경변수로 덮어쓸 수 있다. 토큰을 명시적으로 빈 값으로 덮어쓰면 시작에 실패한다.

| 항목 | 설정 |
|---|---|
| 이미지 | 위에서 빌드·업로드한 태그 |
| `JUPYTER_TOKEN` | 기본 `default`. 운영에서는 플랫폼 Secret으로 덮어씀. 빈 값이면 시작 실패 |
| `JUPYTER_ROOT_DIR` | 기본 `/workspace/pv`. 변경 시 실제 공유 PVC 마운트 경로와 일치시킴 |
| 포트 | 기본 `8888`. Service 대상 포트도 일치시킴 |
| 실행 사용자 | UID/GID `1000:1000`. PVC에 디렉토리·파일 생성 및 수정 권한 필요 |
| 시작 명령 | 이미지 ENTRYPOINT 그대로 사용 |
| 서버 구성 | 서버별 replicas `1`, Executor가 접근할 수 있는 고유 Service 주소 |

기본값을 변경하려면 Kubernetes의 같은 namespace에 `jupyter-config` ConfigMap과
`jupyter-secret` Secret을 만들고 Deployment의 컨테이너에 아래처럼 연결한다.
각 리소스에는 아래 `key`와 동일한 이름으로 값을 등록한다.
리소스를 만들기만 하거나 파일로 마운트하는 것으로는 환경변수가 주입되지 않는다.

```yaml
# Deployment의 spec.template.spec.containers[] 항목 아래
env:
  - name: JUPYTER_ROOT_DIR
    valueFrom:
      configMapKeyRef:
        name: jupyter-config
        key: JUPYTER_ROOT_DIR
  - name: JUPYTER_TOKEN
    valueFrom:
      secretKeyRef:
        name: jupyter-secret
        key: JUPYTER_TOKEN
```

이렇게 주입한 값이 Dockerfile 기본값보다 우선한다. 루트를 변경하면
`volumeMounts.mountPath`도 동일하게 지정하고 쓰기 권한을 확인한다.
환경변수만 변경한다고 PVC 마운트 위치가 자동으로 바뀌지는 않는다.
ConfigMap·Secret 값 변경 후에는 Pod를 재생성해야 새 환경변수가 적용된다.

여러 Jupyter 서버는 동일한 공유 스토리지의 동일한 작업공간을 바라보도록 배포한다.
이 스토리지는 Agent/Executor 공유 스토리지와 별개다. 서로 다른 서버의 커널 세션이
섞이지 않도록 여러 Jupyter Pod를 하나의 Service로 무작위 분산하지 않는다.

`HOME=/home/jovyan`은 Jupyter 설정·사용자 데이터 경로이며 작업공간 루트와 다르다.
홈 전체에 빈 볼륨을 마운트하면 내장 `.jupyter/jupyter_server_config.py`가 가려지므로
주의한다. read-only root filesystem을 사용하면 설정을 보존하면서 Jupyter가 쓰는
홈 하위 경로 및 `/tmp`의 쓰기 공간도 별도로 제공해야 한다.

배포 후 Executor에 해당 서버의 endpoint와 토큰을 등록한다.
Executor에서 REST 및 WebSocket으로 해당 서버에 접근할 수 있어야 한다.
