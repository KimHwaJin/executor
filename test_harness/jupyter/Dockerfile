ARG PYTHON310_IMAGE=astral/uv:python3.10-bookworm-slim
ARG PYTHON311_IMAGE=astral/uv:python3.11-bookworm-slim
ARG PYTHON312_IMAGE=astral/uv:python3.12-bookworm-slim
ARG UV_DEFAULT_INDEX=https://pypi.org/simple

# Each environment installs the pins exported from the deployment lock, so the
# harness and the delivered image cannot drift. Exported pins also install
# from a mirror other than the one the lock was made with.
FROM ${PYTHON310_IMAGE} AS python310

ARG UV_DEFAULT_INDEX

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY deploy/jupyter/environments/3102311 /opt/jupyter-env/3102311

RUN uv export --project /opt/jupyter-env/3102311 --frozen --no-dev \
        --no-emit-project --no-hashes --quiet \
        --output-file /opt/jupyter-env/3102311/requirements.txt \
    && uv venv --no-project --clear \
        --python /usr/local/bin/python3.10 /opt/venvs/3102311 \
    && uv pip install --strict --python /opt/venvs/3102311/bin/python \
        --requirements /opt/jupyter-env/3102311/requirements.txt

FROM ${PYTHON311_IMAGE} AS python311

ARG UV_DEFAULT_INDEX

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY deploy/jupyter/environments/default /opt/jupyter-env/default

RUN uv export --project /opt/jupyter-env/default --frozen --no-dev \
        --no-emit-project --no-hashes --quiet \
        --output-file /opt/jupyter-env/default/requirements.txt \
    && uv venv --no-project --clear \
        --python /usr/local/bin/python3.11 /opt/venvs/default \
    && uv pip install --strict --python /opt/venvs/default/bin/python \
        --requirements /opt/jupyter-env/default/requirements.txt

FROM ${PYTHON312_IMAGE}

ARG UV_DEFAULT_INDEX

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venvs/jupyter/bin:${PATH}" \
    HOME=/home/jovyan \
    JUPYTER_CONFIG_DIR=/home/jovyan/.jupyter \
    JUPYTER_PATH=/opt/venvs/jupyter/share/jupyter \
    JUPYTER_ROOT_DIR=/workspace/pv \
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
COPY --from=python311 /usr/local/bin/python3.11 /usr/local/bin/python3.11
COPY --from=python311 /usr/local/lib/libpython3.11.so.1.0 /usr/local/lib/libpython3.11.so.1.0
COPY --from=python311 /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=python311 /opt/venvs/default /opt/venvs/default

COPY deploy/jupyter/environments/server /opt/jupyter-env/server

RUN ldconfig \
    && uv export --project /opt/jupyter-env/server --frozen --no-dev \
        --no-emit-project --no-hashes --quiet \
        --output-file /opt/jupyter-env/server/requirements.txt \
    && uv venv --no-project --clear \
        --python /usr/local/bin/python3.12 /opt/venvs/jupyter \
    && uv pip install --strict --python /opt/venvs/jupyter/bin/python \
        --requirements /opt/jupyter-env/server/requirements.txt

COPY deploy/jupyter/extension /opt/jupyter-resource-extension
COPY deploy/jupyter/executor_resource_extension.json \
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
    && mkdir -p /workspace/pv /home/jovyan/.jupyter \
    && chown -R 1000:1000 /workspace/pv /home/jovyan

COPY --chown=1000:1000 deploy/jupyter/jupyter_server_config.py \
    /home/jovyan/.jupyter/jupyter_server_config.py
COPY --chmod=755 deploy/jupyter/start-jupyter.sh /usr/local/bin/start-jupyter

USER 1000:1000
WORKDIR /workspace/pv

EXPOSE 8888

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-jupyter"]
