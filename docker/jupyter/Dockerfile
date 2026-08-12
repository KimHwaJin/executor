FROM jupyter/datascience-notebook:latest

USER root

COPY docker/jupyter_resource_extension /opt/jupyter-resource-extension

RUN python -m pip install \
        --no-cache-dir \
        --no-deps \
        --no-build-isolation \
        /opt/jupyter-resource-extension \
    && jupyter server extension enable \
        --py executor_resource_extension \
        --sys-prefix \
    && python -c "import executor_resource_extension"

USER ${NB_UID}
