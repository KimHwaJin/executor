# docker image
FROM harbor-srep01.skhynix.com/aix-harbor/python-sqlite3-oracle:gaia-f22.0-v0.3

# env directory
ENV USER_DIR=/root
ENV WORK_DIR=/app
ENV LD_LIBRARY_PATH=/home/instant_client/instantclient_23_9

ENV HCP_ACTIVE_PROFILE=development

# file copy
COPY ./ $WORK_DIR/
# working directory
WORKDIR $WORK_DIR
RUN pip install -r requirements.txt
# requirements.txt dependency install is handled by runtime_requirements.py at app startup using uv.
# Keep Docker build fast by not installing requirements in CICD.


ENV RESET_DB_ON_START=true \
    RUN_MIGRATIONS_ON_START = true \
    DB_WAIT_SECONDS=60
    
# port
EXPOSE 5000
# run
ENTRYPOINT ["python", "app.py"]