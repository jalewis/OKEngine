# okengine.embeddings sidecar image. Build, then pin the resulting digest into
# ../extension.yaml (entrypoint.image.digest) before enabling — see ../README.md.
#
#   docker build -t registry.example.com/okengine/embeddings:0.1.0 .
#   docker push  registry.example.com/okengine/embeddings:0.1.0
#   docker inspect --format '{{index .RepoDigests 0}}' registry.example.com/okengine/embeddings:0.1.0
#
FROM python:3.12-slim
WORKDIR /app
COPY run.py /app/run.py
# stdlib-only reference (token-cosine). For production, add your embeddings model here
# (e.g. sentence-transformers / fastembed) and swap the similarity core in run.py.
USER 65534:65534
ENTRYPOINT ["python", "/app/run.py"]
