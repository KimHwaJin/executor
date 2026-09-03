ARG PYTHON310_IMAGE=ghcr.io/astral-sh/uv:0.12.8-python3.10-bookworm-slim
ARG PYTHON311_IMAGE=ghcr.io/astral-sh/uv:0.12.8-python3.11-bookworm-slim
ARG UV_DEFAULT_INDEX=https://pypi.org/simple

FROM ${PYTHON310_IMAGE} AS python310

ARG UV_DEFAULT_INDEX

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY environments/3102311 /opt/jupyter-env/3102311

RUN UV_PROJECT_ENVIRONMENT=/opt/venvs/3102311 \
    uv sync --project /opt/jupyter-env/3102311 \
        --locked --no-dev --no-install-project \
        --python /usr/local/bin/python3.10

FROM ${PYTHON311_IMAGE}

ARG UV_DEFAULT_INDEX

# Deployment settings: override these with container environment variables.
# ROOT_DIR must match the shared PVC mount path and be writable by UID 1000.
# TOKEN defaults to "default" for testing; override via Secret in production.
# See README.md section 5 before deployment.
ENV JUPYTER_ROOT_DIR=/workspace/jupyter \
    JUPYTER_TOKEN=default

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venvs/jupyter/bin:${PATH}" \
    HOME=/home/jovyan \
    JUPYTER_CONFIG_DIR=/home/jovyan/.jupyter \
    JUPYTER_PATH=/opt/venvs/jupyter/share/jupyter \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        libgomp1 \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python310 /usr/local/bin/python3.10 /usr/local/bin/python3.10
COPY --from=python310 /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.10.so.1.0
COPY --from=python310 /usr/local/lib/python3.10 /usr/local/lib/python3.10
COPY --from=python310 /opt/venvs/3102311 /opt/venvs/3102311

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

COPY extension /opt/jupyter-resource-extension
COPY executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json

RUN uv pip install --strict --python /opt/venvs/jupyter/bin/python \
        --no-deps /opt/jupyter-resource-extension \
    && /opt/venvs/default/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=default \
        --display-name="Default (Python 3.11)" \
    && /opt/venvs/3102311/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=3102311 \
        --display-name="3102311 (Python 3.10)" \
    && rm -rf /opt/venvs/jupyter/share/jupyter/kernels/python3

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

COPY --chown=1000:1000 jupyter_server_config.py \
    /home/jovyan/.jupyter/jupyter_server_config.py
COPY --chmod=755 start-jupyter.sh /usr/local/bin/start-jupyter

USER 1000:1000
WORKDIR "${JUPYTER_ROOT_DIR}"

EXPOSE 8888

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-jupyter"]
