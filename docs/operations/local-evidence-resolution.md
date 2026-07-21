# Local evidence resolution

`okengine.assessments/local_evidence.py` normalizes evidence already present in a composed vault
for CHE assessment producers. It never accesses the network. Missing URLs, pages, and identifiers
are returned for an explicit collection operation; a local search does not claim internet-wide
coverage.

The contract keeps source identity separate from ingestion provenance. An article repository can
identify how an article reached the vault without becoming its publisher. Authority requires structured
authority identity; a publisher label alone is insufficient. Alias matches are retained for
discovery with `identity_transfer: required` until a domain producer verifies a suitable identity
edge.

Results contain resolved artifacts, unresolved references with stable reasons, malformed inputs,
bounded search counts, held alias matches, and a deterministic snapshot digest.
Packs may pass `embedded_article_fields` to map their own schema fields into the generic embedded
article contract; this keeps deployment-specific repository vocabulary outside the engine.
