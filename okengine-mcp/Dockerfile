# okengine-mcp — slim, standalone MCP query surface over the OKF vault.
# Deliberately NOT the hermes-agent image: that image's s6 init starts a full
# gateway in every container (duplicate cron ticker + Telegram poller). This
# image runs ONLY the MCP server. Read-only over the vault.
#
# Build context is the repo ROOT (so the engine's kb_* wrappers are COPYable
# without vendoring/duplication): build with
#   docker compose build okengine-mcp     (compose sets context: .)
#
# Trixie base (glibc 2.41): the prebuilt IWE binary needs GLIBC >= 2.39.
# Base pinned by digest for reproducibility (this also pins the apt snapshot, so
# package versions don't float). To refresh: see docs/supply-chain.md.
# python:3.13-slim-trixie as of 2026-06.
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f

WORKDIR /app

# nodejs/npm — qmd is a node CLI (runtime dep, not just install-time).
# curl — fetch the IWE binary. ripgrep — fast lexical paths.
# build-essential + python3-setuptools — qmd's better-sqlite3 native module
# compiles via node-gyp at install (no prebuilt for this node/platform); py3.13
# dropped distutils, so setuptools provides the shim node-gyp's gyp needs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs npm curl ripgrep ca-certificates \
        build-essential python3 python3-setuptools \
    && rm -rf /var/lib/apt/lists/*

# IWE — markdown knowledge-graph CLI (powers find_references). Read-only.
# Pinned version + sha256-verified download (the release ships no checksums file;
# the hash is recorded here — see docs/supply-chain.md to bump).
ARG IWE_VERSION=0.3.2
ARG IWE_SHA256=7e90a698b0bb2474b7aaf9beab275d6da842dc38c9dd75e323ab1d4eadbce63d
RUN curl -fsSL --retry 3 -o /tmp/iwe.tgz \
      "https://github.com/iwe-org/iwe/releases/download/iwe-v${IWE_VERSION}/iwe-v${IWE_VERSION}-x86_64-unknown-linux-gnu.tar.gz" && \
    echo "${IWE_SHA256}  /tmp/iwe.tgz" | sha256sum -c - && \
    tar -xzf /tmp/iwe.tgz -C /usr/local/bin iwe && \
    chmod +x /usr/local/bin/iwe && rm -f /tmp/iwe.tgz && iwe --version

# qmd — local hybrid search (BM25 + vector + rerank). Index + GGUF models live on
# the mounted /opt/data/qmd (not baked); this installs the CLI only.
# Debian's nodejs bundles a broken node-gyp (its vendored gyp/ is missing →
# "No module named 'gyp'"). Install a self-contained node-gyp and point npm AND
# the env at it, then compile qmd's better-sqlite3 against it.
ENV PYTHON=/usr/bin/python3
RUN npm install -g node-gyp@11 \
 && export npm_config_node_gyp="$(npm root -g)/node-gyp/bin/node-gyp.js" \
 && npm install -g @tobilu/qmd@2.5.3 \
 && qmd --version

COPY okengine-mcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY okengine-mcp/server.py .
# Engine wrappers (source of truth: scripts/cron/) — copied, not vendored.
COPY scripts/cron/kb_search.py scripts/cron/kb_graph.py scripts/cron/tier_lib.py ./scripts/

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
