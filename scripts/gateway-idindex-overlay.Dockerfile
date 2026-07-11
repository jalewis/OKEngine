# Fast gateway hotfix overlay for the id-index write-path fix (scripts/cron/id_index.py).
#
# WHY THIS EXISTS: write_server runs IN the gateway and imports id_index from the BAKED
# /opt/hermes/scripts/cron/id_index.py — NOT the staged /opt/data/scripts copy that
# deploy-cron-scripts.sh reaches (that copy only feeds the CRON). So a change to a scripts/cron
# module that write_server imports (id_index, id_lib, schema_lib, converge) does not go live by
# staging; it needs the gateway image. This thin overlay rebakes the one changed file onto the
# CURRENT gateway image without re-running build-engine-image.sh (which re-clones Hermes from
# GitHub). The next proper build-engine-image.sh release bakes it canonically, so there is no
# drift to track.
#
# USAGE (from the engine repo root):
#   docker tag hermes-agent:latest hermes-agent:pre-idindex-hotfix                       # backup, reversible
#   docker build -f scripts/gateway-idindex-overlay.Dockerfile -t hermes-agent:latest .
#   (cd <deploy> && ENGINE_DIR=<engine-dir> docker compose up -d --force-recreate --no-deps gateway)
#
# Pin BASE to a versioned tag instead of :latest with --build-arg BASE=hermes-agent:okengine-<ver>.
ARG BASE=hermes-agent:latest
FROM ${BASE}
COPY scripts/cron/id_index.py /opt/hermes/scripts/cron/id_index.py
