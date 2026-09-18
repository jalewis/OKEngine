# okengine-reader — standalone read-only vault reader. No hermes coupling.
# Trixie base pinned by digest for reproducibility (also pins the apt snapshot);
# the weasyprint PDF stack targets trixie. To refresh: see docs/supply-chain.md.
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

WORKDIR /app

# pandoc (md->docx); ripgrep (keyword search); curl.
# PDF engine: weasyprint (pip) — wkhtmltopdf was dropped from Debian trixie.
# libpango/harfbuzz/fonts are weasyprint's runtime deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc ripgrep curl \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi8 \
        libjpeg62-turbo libopenjp2-7 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# NB: backlinks are built by an in-process link-scanner (okengine#179) — no iwe binary needed.

COPY okengine-reader/requirements.txt .
COPY --from=wheel /wheel/ /wheel/
RUN pip install --no-cache-dir -r requirements.txt /wheel/*.whl

COPY okengine-reader/*.py ./
COPY okengine-reader/static/ ./static/

ENV VAULT_DIR=/vault PORT=9200
EXPOSE 9200

# Run as non-root; the vault is mounted read-only so no write access is possible.
RUN useradd -u 10001 -m reader
USER reader

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
