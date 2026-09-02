FROM python:3.10.11-slim-bullseye AS python310

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY environments/3102311/requirements.txt /opt/jupyter-env/3102311/requirements.txt
COPY pip.conf /etc/pip.conf

RUN python3.10 -m venv --copies /opt/venvs/3102311 \
    && /opt/venvs/3102311/bin/pip install --no-cache-dir "ipykernel>=6.30,<7" \
    && if [ -s /opt/jupyter-env/3102311/requirements.txt ]; then \
        /opt/venvs/3102311/bin/pip install --no-cache-dir \
            -r /opt/jupyter-env/3102311/requirements.txt; \
    fi

FROM python:3.11-slim-bullseye

# Deployment settings: override these with container environment variables.
# ROOT_DIR must match the shared PVC mount path and be writable by UID 1000.
# TOKEN defaults to "default" for testing; override via Secret in production.
# See README.md section 4 before deployment.
ENV JUPYTER_ROOT_DIR=/workspace/jupyter \
    JUPYTER_TOKEN=default

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venvs/jupyter/bin:${PATH}" \
    HOME=/home/jovyan \
    JUPYTER_CONFIG_DIR=/home/jovyan/.jupyter \
    JUPYTER_PATH=/opt/venvs/jupyter/share/jupyter

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        libbz2-1.0 \
        libdb5.3 \
        libexpat1 \
        libgdbm6 \
        libgomp1 \
        libgssapi-krb5-2 \
        liblzma5 \
        libncursesw6 \
        libnsl2 \
        libreadline8 \
        libsqlite3-0 \
        libtirpc3 \
        libuuid1 \
        tini \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python310 /usr/local/bin/python3.10 /usr/local/bin/python3.10
COPY --from=python310 /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.10.so.1.0
COPY --from=python310 /usr/local/lib/python3.10 /usr/local/lib/python3.10
COPY --from=python310 /opt/venvs/3102311 /opt/venvs/3102311

COPY environments/server/requirements.txt /opt/jupyter-env/server/requirements.txt
COPY environments/default/requirements.txt /opt/jupyter-env/default/requirements.txt
COPY pip.conf /etc/pip.conf

RUN ldconfig \
    && /usr/local/bin/python3.11 -m venv /opt/venvs/jupyter \
    && /usr/local/bin/python3.11 -m venv /opt/venvs/default \
    && /opt/venvs/jupyter/bin/pip install --no-cache-dir \
        -r /opt/jupyter-env/server/requirements.txt \
    && /opt/venvs/default/bin/pip install --no-cache-dir \
        "ipykernel>=6.30,<7" \
    && /opt/venvs/default/bin/pip install --no-cache-dir \
        -r /opt/jupyter-env/default/requirements.txt

COPY extension /opt/jupyter-resource-extension
COPY executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json

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
