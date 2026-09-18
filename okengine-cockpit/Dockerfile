# okengine-cockpit — standalone read-only "intelligence cockpit" vault reader. No hermes coupling.
# Trixie base pinned by digest for reproducibility; the weasyprint PDF stack targets trixie.
ARG PYTHON_BASE_IMAGE=python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f
# Node from the official image, not Debian's `nodejs npm` -- the same change as okengine-mcp, and
# for the same reason: Debian's npm drags in hundreds of node-* packages. Measured here as 544 apt
# packages and an 83s build, against 168 and 43s. The deck render it serves was checked directly,
# not inferred from a version string: a two-slide marp deck rendered to PDF through chromium as the
# non-root reader (uid 10001) under both, producing byte-for-byte the same 7084-byte output.
ARG NODE_BASE_IMAGE=node@sha256:4d676821dff059fd00d277ee4261ef34ea712317fed0737c03941481b5760c96
FROM ${NODE_BASE_IMAGE} AS node

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

# marp-cli (+ headless chromium) renders the marp deck `.md`s that pdf-enabled streams serve, on
# demand — the pinned gateway ships no browser, so the render lives here. CHROME_PATH points at a
# wrapper that forces --no-sandbox (chromium can't use its sandbox as non-root inside a container).
# node is a runtime dependency of marp. It links libstdc++, which this image has no build-essential
# to supply; chromium's own dependencies bring it in. That is transitive, so the render above is
# the evidence it holds -- if chromium ever stops pulling libstdc++, `marp` fails at startup.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
    && npm install -g @marp-team/marp-cli@4.4.1 \
    && npm cache clean --force \
    && printf '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"\n' \
         > /usr/local/bin/chromium-nosandbox \
    && chmod +x /usr/local/bin/chromium-nosandbox \
    && rm -rf /var/lib/apt/lists/*
ENV CHROME_PATH=/usr/local/bin/chromium-nosandbox

# NB: backlinks are built by an in-process link-scanner (okengine#179) — no iwe binary needed.

COPY okengine-cockpit/requirements.txt .
COPY --from=wheel /wheel/ /wheel/
RUN pip install --no-cache-dir -r requirements.txt /wheel/*.whl

COPY okengine-cockpit/*.py ./
COPY okengine-cockpit/static/ ./static/

ENV VAULT_DIR=/vault PORT=9200
EXPOSE 9200

# Run as non-root; the vault is mounted read-only so no write access is possible.
RUN useradd -u 10001 -m reader
USER reader

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
