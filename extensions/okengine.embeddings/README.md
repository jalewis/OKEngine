# okengine.embeddings  (sidecar reference)

The canonical first-party **sidecar** extension — the worked example of running optional
(potentially untrusted, third-party) extension code in an **isolated, hardened container** rather
than in the gateway.

**What it does:** computes content-similarity over entity pages and surfaces **semantic
near-duplicate candidates** — the layer above `okengine.dedupe`'s name/alias detection (two
entities that are clearly the same thing but share no name/alias still get caught). Candidates are
written back via the scoped write MCP.

**How it runs (the contract + #124 hardening):** the deploy materializes a compose service for it
(`render_sidecar_service`). The image is **digest-pinned** (content integrity), and the container
is confined: **no host network** (joins the per-pack bridge, reaches the MCP by service name —
#138), **`cap_drop: ALL`** + **`no-new-privileges`**, **read-only rootfs** + `/tmp` tmpfs, and
**pid/mem/cpu caps** (#124). Its only vault access is the scoped read/write MCP (#132), injected
as env (`OKENGINE_MCP_URL` / `OKENGINE_READ_TOKEN` / `OKENGINE_WRITE_MCP_URL` /
`OKENGINE_WRITE_TOKEN`). So untrusted code here can neither escalate nor touch the host.

## Enable it (build → pin → enable)

It ships as a **buildable template** — a sidecar will not pull without a real pinned digest:

```bash
cd extensions/okengine.embeddings/image
docker build -t registry.example.com/okengine/embeddings:0.1.0 .
docker push  registry.example.com/okengine/embeddings:0.1.0
docker inspect --format '{{index .RepoDigests 0}}' registry.example.com/okengine/embeddings:0.1.0
# → copy the sha256:… into extension.yaml entrypoint.image.digest (replacing the placeholder)

framework extensions enable <pack> okengine.embeddings
```

## What's reference vs. yours

- **Real + tested:** the similarity core (`cosine`, `find_similar_pairs`, token-cosine over page text).
- **Operator TODO (stubs that demonstrate the env contract):** `fetch_entities` (page the read
  MCP) and `publish` (write `dashboards/similar-entities.md` via the write MCP). For production,
  complete those and swap the token-cosine core for a real embeddings model.

Config: `threshold` (default 0.85). Companion: `okengine.dedupe` (name/alias detection + merge).
