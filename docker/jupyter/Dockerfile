FROM python:3.12-slim-bookworm

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
        libgomp1 \
        python3.11 \
        python3.11-venv \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY docker/jupyter/environments/server/requirements.txt /opt/jupyter-env/server/requirements.txt
COPY docker/jupyter/environments/basic/requirements.txt /opt/jupyter-env/basic/requirements.txt
COPY docker/jupyter/environments/ml/requirements.txt /opt/jupyter-env/ml/requirements.txt

RUN /usr/local/bin/python3.12 -m venv /opt/venvs/jupyter \
    && /usr/bin/python3.11 -m venv /opt/venvs/basic \
    && /usr/local/bin/python3.12 -m venv /opt/venvs/ml \
    && /opt/venvs/jupyter/bin/pip install --no-cache-dir \
        -r /opt/jupyter-env/server/requirements.txt \
    && /opt/venvs/basic/bin/pip install --no-cache-dir \
        -r /opt/jupyter-env/basic/requirements.txt \
    && /opt/venvs/ml/bin/pip install --no-cache-dir \
        -r /opt/jupyter-env/ml/requirements.txt

COPY docker/jupyter_resource_extension /opt/jupyter-resource-extension
COPY docker/jupyter/executor_resource_extension.json \
    /opt/venvs/jupyter/etc/jupyter/jupyter_server_config.d/executor_resource_extension.json

RUN /opt/venvs/jupyter/bin/pip install --no-cache-dir --no-deps \
        /opt/jupyter-resource-extension \
    && /opt/venvs/basic/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=basic \
        --display-name="Basic (Python 3.11)" \
    && /opt/venvs/ml/bin/python -m ipykernel install \
        --prefix=/opt/venvs/jupyter \
        --name=ml \
        --display-name="ML (Python 3.12)" \
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

COPY --chown=1000:1000 docker/jupyter_server_config.py \
    /home/jovyan/.jupyter/jupyter_server_config.py

USER 1000:1000
WORKDIR /workspace/pv

EXPOSE 8888

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/venvs/jupyter/bin/jupyter", "lab"]
