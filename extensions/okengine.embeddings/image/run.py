#!/usr/bin/env python3
"""okengine.embeddings sidecar (okengine#135 reference, #124-hardened).

Reads entity pages via the scoped READ MCP, computes pairwise content similarity, and surfaces
SEMANTIC near-duplicate candidates — complementing okengine.dedupe's name/alias detection —
written back via the scoped WRITE MCP. The endpoints + scoped tokens are injected as env by the
sidecar contract (render_sidecar_service):

  OKENGINE_MCP_URL / OKENGINE_READ_TOKEN          read query surface
  OKENGINE_WRITE_MCP_URL / OKENGINE_WRITE_TOKEN   enforced write path (#132)
  OKENGINE_EXTENSION_ID                           server-side provenance
  OKENGINE_CONFIG_THRESHOLD                       config

This is a REFERENCE TEMPLATE: the similarity core (cosine / find_similar_pairs) is real and
tested; `fetch_entities` / `publish` are minimal operator-TODO stubs that demonstrate the env
contract. For production, complete the MCP read/write paging and swap the token-cosine core for a
real embeddings model (sentence-transformers / fastembed). The container runs confined — dropped
capabilities, no privilege escalation, read-only rootfs, resource-capped — so this untrusted
third-party code can neither escalate nor touch the host (okengine#124)."""
import math
import os
import re
import sys
from collections import Counter

_TOKEN = re.compile(r"[a-z0-9]+")


def vectorize(text: str) -> Counter:
    return Counter(_TOKEN.findall(text.lower()))


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[t] * b[t] for t in (set(a) & set(b)))
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def find_similar_pairs(docs, threshold):
    """docs: list[(slug, text)] -> [(slug_a, slug_b, score)] at/above threshold, score desc.
    O(n^2) reference; for a large vault, batch or use ANN over real embeddings."""
    vecs = [(slug, vectorize(text)) for slug, text in docs]
    out = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            s = cosine(vecs[i][1], vecs[j][1])
            if s >= threshold:
                out.append((vecs[i][0], vecs[j][0], round(s, 3)))
    return sorted(out, key=lambda x: x[2], reverse=True)


def _require_env(name) -> str:
    v = os.environ.get(name, "")
    if not v:
        print(f"embeddings: injected env {name} absent — running outside the sidecar contract?",
              file=sys.stderr)
    return v


def fetch_entities():
    """Operator TODO: page the read MCP (OKENGINE_MCP_URL + OKENGINE_READ_TOKEN) for entity pages,
    return [(slug, text)]. Reference returns [] (no live MCP at build/exemplar time)."""
    _require_env("OKENGINE_MCP_URL")
    _require_env("OKENGINE_READ_TOKEN")
    return []


def publish(pairs):
    """Operator TODO: write candidates to dashboards/similar-entities.md via the write MCP
    (OKENGINE_WRITE_MCP_URL + OKENGINE_WRITE_TOKEN). Reference prints them."""
    _require_env("OKENGINE_WRITE_MCP_URL")
    _require_env("OKENGINE_WRITE_TOKEN")
    print(f"embeddings: {len(pairs)} semantic near-duplicate candidate(s):")
    for a, b, s in pairs[:50]:
        print(f"  {s:>5}  {a}  ~  {b}")


def main() -> int:
    threshold = float(os.environ.get("OKENGINE_CONFIG_THRESHOLD", "0.85"))
    ext = os.environ.get("OKENGINE_EXTENSION_ID", "okengine.embeddings")
    print(f"[{ext}] semantic dedupe candidates · threshold={threshold}")
    publish(find_similar_pairs(fetch_entities(), threshold))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
