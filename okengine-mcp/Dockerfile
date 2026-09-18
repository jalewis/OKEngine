# okengine-mcp — slim, standalone MCP query surface over the OKF vault.
# Deliberately NOT the hermes-agent image: that image's s6 init starts a full
# gateway in every container (duplicate cron ticker + Telegram poller). This
# image runs ONLY the MCP server. Read-only over the vault.
#
# Build context is the repo ROOT (so the engine's kb_* wrappers are COPYable
# without vendoring/duplication): build with
#   docker compose build okengine-mcp     (compose sets context: .)
#
# Trixie base, pinned for reproducibility.
# Base pinned by digest for reproducibility (this also pins the apt snapshot, so
# package versions don't float). To refresh: see docs/supply-chain.md.
# python:3.13-slim-trixie as of 2026-06.
ARG PYTHON_BASE_IMAGE=python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f
# Node comes from the official image, NOT Debian's `nodejs npm`. Debian packages every one of npm's
# JavaScript dependencies as its own .deb, so `apt-get install npm` pulled 340 node-* packages to
# deliver a tool used for two `npm install -g` lines -- measured 446 apt packages and a 187s build
# with it, 77 packages and 67s without. It also installed Node v20.19.2, while @tobilu/qmd@2.5.3
# declares `engines: node >= 22.0.0` (the build logged EBADENGINE and carried on). This is Node
# v22.23.2, and qmd builds and runs on it with no engine warning. Pinned by linux/amd64 platform
# digest; release CI overrides it with the byte-identical copy in the project registry (see
# OKENGINE_NODE_BASE_IMAGE).
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

# node + npm — qmd is a node CLI, a RUNTIME dependency, so node stays in this image. The official
# image links libstdc++ and libgcc_s, which build-essential below provides here.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# ripgrep — fast lexical paths.
# build-essential + python3-setuptools — qmd's better-sqlite3 native module
# compiles via node-gyp at install; py3.13 dropped distutils, so setuptools
# provides the shim node-gyp's gyp needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ripgrep ca-certificates \
        build-essential python3 python3-setuptools \
    && rm -rf /var/lib/apt/lists/*

# qmd — local hybrid search (BM25 + vector + rerank). Index + GGUF models live on
# the mounted /opt/data/qmd (not baked); this installs the CLI only.
# A standalone node-gyp compiles qmd's better-sqlite3. It was originally added because Debian's
# nodejs shipped a broken node-gyp ("No module named 'gyp'"); that package is no longer used, but
# the explicit node-gyp is kept because it is the path verified to build qmd -- dropping it is a
# separate change that needs its own build proof.
ENV PYTHON=/usr/bin/python3
RUN npm install -g node-gyp@11 \
 && export npm_config_node_gyp="$(npm root -g)/node-gyp/bin/node-gyp.js" \
 && npm install -g @tobilu/qmd@2.5.3 \
 && qmd --version

COPY okengine-mcp/requirements.txt .
COPY --from=wheel /wheel/ /wheel/
RUN pip install --no-cache-dir -r requirements.txt /wheel/*.whl

COPY okengine-mcp/server.py .
# Engine wrappers (source of truth: scripts/cron/) — copied, not vendored.
COPY scripts/cron/kb_search.py scripts/cron/tier_lib.py scripts/cron/schema_lib.py ./scripts/
COPY config/base-schema.yaml ./config/base-schema.yaml

ENV WIKI_PATH=/opt/vault \
    OKENGINE_MCP_SCRIPTS=/app/scripts \
    OKENGINE_MCP_PY=python3 \
    OKENGINE_MCP_TRANSPORT=streamable-http \
    OKENGINE_MCP_HOST=0.0.0.0 \
    PORT=8730
EXPOSE 8730

# Non-root; compose pins user to the vault/data owner (HERMES_UID) so qmd can
# read+write its SQLite cache on /opt/data/qmd while the vault stays read-only.
CMD ["python", "server.py"]
