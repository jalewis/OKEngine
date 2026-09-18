ARG PYTHON_BASE_IMAGE=python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f
FROM ${PYTHON_BASE_IMAGE} AS wheel
WORKDIR /build
COPY src/ ./src/
COPY tools/schema_validator.py tools/policy_plane.py ./tools/
COPY okengine-mcp/output_contract_enforce.py okengine-mcp/converge.py ./okengine-mcp/
COPY scripts/cron/id_lib.py scripts/cron/schema_lib.py scripts/cron/id_index.py scripts/cron/okf_migrate.py ./scripts/cron/
COPY scripts/build_engine_wheel.py ./scripts/build_engine_wheel.py
COPY config/base-schema.yaml ./config/base-schema.yaml
RUN python scripts/build_engine_wheel.py --out /wheel

FROM ${PYTHON_BASE_IMAGE}

WORKDIR /engine
COPY okengine-mcp/requirements.txt /engine/requirements.txt
COPY --from=wheel /wheel/ /wheel/
RUN pip install --no-cache-dir -r /engine/requirements.txt /wheel/*.whl

COPY scripts/framework_operations.py /engine/scripts/framework_operations.py
COPY scripts/operation_run.py /engine/scripts/operation_run.py
COPY engine-manifest.yaml /engine/engine-manifest.yaml
COPY extensions/ /engine/extensions/
COPY okengine-operations/app.py /engine/okengine-operations/app.py

ENV WIKI_PATH=/opt/vault PORT=8732 OKENGINE_SOURCE_ROOT=/engine
EXPOSE 8732

RUN useradd -u 10001 -m opsrunner
USER opsrunner

CMD ["sh", "-c", "uvicorn app:app --app-dir /engine/okengine-operations --host 0.0.0.0 --port ${PORT}"]
