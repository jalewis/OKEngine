FROM python:3.13-slim-trixie@sha256:c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f

WORKDIR /engine
COPY okengine-mcp/requirements.txt /engine/requirements.txt
RUN pip install --no-cache-dir -r /engine/requirements.txt

COPY scripts/framework_operations.py /engine/scripts/framework_operations.py
COPY extensions/ /engine/extensions/
COPY okengine-operations/app.py /engine/okengine-operations/app.py

ENV WIKI_PATH=/opt/vault PORT=8732
EXPOSE 8732

RUN useradd -u 10001 -m opsrunner
USER opsrunner

CMD ["sh", "-c", "uvicorn app:app --app-dir /engine/okengine-operations --host 0.0.0.0 --port ${PORT}"]
