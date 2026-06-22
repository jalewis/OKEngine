# okengine-reader — standalone read-only vault reader. No hermes coupling.
# Trixie base (glibc 2.41): the prebuilt IWE binary requires GLIBC >= 2.39,
# which bookworm-slim (2.36) lacks. Pinned by digest for reproducibility (also
# pins the apt snapshot). To refresh: see docs/supply-chain.md.
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f

WORKDIR /app

# pandoc (md->docx); ripgrep (keyword search); curl (fetch IWE binary).
# PDF engine: weasyprint (pip) — wkhtmltopdf was dropped from Debian trixie.
# libpango/harfbuzz/fonts are weasyprint's runtime deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc ripgrep curl \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi8 \
        libjpeg62-turbo libopenjp2-7 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# IWE — markdown knowledge-graph CLI; powers the "what links here" backlinks
# panel (reads the vault's [[wikilinks]] via wiki/.iwe/config.toml). Read-only,
# stateless — safe on the /vault:ro mount. Only the `iwe` CLI is needed here.
# Pinned version + sha256-verified download (see docs/supply-chain.md to bump).
ARG IWE_VERSION=0.3.2
ARG IWE_SHA256=7e90a698b0bb2474b7aaf9beab275d6da842dc38c9dd75e323ab1d4eadbce63d
RUN curl -fsSL --retry 3 -o /tmp/iwe.tgz \
      "https://github.com/iwe-org/iwe/releases/download/iwe-v${IWE_VERSION}/iwe-v${IWE_VERSION}-x86_64-unknown-linux-gnu.tar.gz" && \
    echo "${IWE_SHA256}  /tmp/iwe.tgz" | sha256sum -c - && \
    tar -xzf /tmp/iwe.tgz -C /usr/local/bin iwe && \
    chmod +x /usr/local/bin/iwe && rm -f /tmp/iwe.tgz && \
    iwe --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py limits.py .
COPY static/ ./static/

ENV VAULT_DIR=/vault PORT=9200
EXPOSE 9200

# Run as non-root; the vault is mounted read-only so no write access is possible.
RUN useradd -u 10001 -m reader
USER reader

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
