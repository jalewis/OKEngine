# okengine-reader — standalone read-only vault reader. No hermes coupling.
# Trixie base pinned by digest for reproducibility (also pins the apt snapshot);
# the weasyprint PDF stack targets trixie. To refresh: see docs/supply-chain.md.
FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f

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
