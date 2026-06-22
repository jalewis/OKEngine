# Supply chain & dependency pinning

OKEngine's build is pinned so a given engine release builds reproducibly. This
documents what is pinned, where, and how to bump each dependency safely.

## What's pinned

| Dependency | Where | Pin | Integrity |
|---|---|---|---|
| **Base image** (`python:3.13-slim-trixie`) | `okengine-mcp/Dockerfile`, `okengine-reader/Dockerfile` | digest `sha256:c33f0bc4…105e4f` | digest pin (also pins the apt snapshot) |
| **IWE** binary | both Dockerfiles | `IWE_VERSION=0.3.2` | `IWE_SHA256` verified with `sha256sum -c` |
| **qmd** (npm) | `okengine-mcp/Dockerfile` | `@tobilu/qmd@2.5.3` | npm version pin |
| **node-gyp** (npm) | `okengine-mcp/Dockerfile` | `node-gyp@11` | npm version pin |
| **MCP Python deps** | `okengine-mcp/requirements.txt` | `==` pins (`mcp`, `PyYAML`, `uvicorn`) | exact versions |
| **Reader Python deps** | `okengine-reader/requirements.txt` | `==` pins | exact versions |
| **Hermes-Agent** runtime | `engine-manifest.yaml` → `runtime.pinned_sha` | tag `v2026.6.5` → commit `3c231eb…8b43` | `build-engine-image.sh` verifies the clone matches |
| **cron-plus** plugin (required runtime scheduler) | `engine-manifest.yaml` → `dependencies.cron-plus`, `INSTALL.md` §4 | commit `eacd1729…39eff` (untagged) | operator clones + `git checkout` the pin |

apt packages are intentionally **not** version-pinned individually: the base-image
digest pins the Debian snapshot they come from, so they're reproducible without the
brittleness of per-package version pins that disappear from the mirrors.

## Updating a dependency

Bump deliberately, one at a time, and rebuild + test (`make check` + a real image
build) before committing.

### Base image (digest)
The tag (`python:3.13-slim-trixie`) moves when upstream rebuilds (e.g. for CVE
fixes). To adopt a new snapshot, re-resolve the digest and update **both**
Dockerfiles:
```sh
TOKEN=$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -fsS -I -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://registry-1.docker.io/v2/library/python/manifests/3.13-slim-trixie | grep -i docker-content-digest
```

### IWE
Pick the new release tag from <https://github.com/iwe-org/iwe/releases>, then
record its sha256 (the release ships no checksums file, so compute it):
```sh
V=0.3.3
curl -fsSL -o /tmp/iwe.tgz "https://github.com/iwe-org/iwe/releases/download/iwe-v$V/iwe-v$V-x86_64-unknown-linux-gnu.tar.gz"
sha256sum /tmp/iwe.tgz
```
Update `IWE_VERSION` + `IWE_SHA256` in both Dockerfiles. (The build verifies the
hash and fails the build on mismatch.)

### qmd / node-gyp
```sh
npm view @tobilu/qmd version        # latest
```
Pin the chosen version in `okengine-mcp/Dockerfile`.

### Python deps
Edit the `==` pin in the relevant `requirements.txt`. For `mcp`, verify the new
version against `okengine-mcp/server.py` + `write_server.py` (it uses
`mcp.server.fastmcp.FastMCP` + `streamable_http_app()`), since the MCP SDK changes
its API across minor versions.

### Hermes-Agent (the pinned runtime)
This is the biggest bump — it can require rebasing the carried `patches/`.
1. Choose the new upstream tag.
2. Resolve the commit it points to and record both in `engine-manifest.yaml`
   (`runtime.pinned_tag` + `runtime.pinned_sha`):
   ```sh
   git ls-remote --tags https://github.com/NousResearch/hermes-agent.git <new-tag>
   # for an annotated tag, dereference to the commit it points at (the ^{} ref)
   ```
3. Re-run `patches/apply.sh` against the new clone; rebase any patch that fails.
4. `bash scripts/build-engine-image.sh` — it clones the tag and **fails** unless
   the clone's `HEAD` equals `pinned_sha`, so a moved/retagged upstream can't slip
   in unnoticed. Update `pinned_version`/`pinned_tag`/`pinned_sha` together.

### cron-plus (the runtime scheduler plugin)
A separate required Hermes plugin the operator clones into
`~/.hermes/plugins/cron-plus` (INSTALL.md §4). It is untagged, so it is pinned by
commit. To bump: pick the new commit, update
`engine-manifest.yaml` `dependencies.cron-plus.pinned_sha` and the `git checkout`
SHA in INSTALL.md §4, then re-clone/checkout on the host.
```sh
git ls-remote https://github.com/jalewis/hermes-cron-plus.git HEAD
```

## Notes / known gaps
- The IWE download is **x86_64-linux only** (matches the build). An arm64 build
  would need the `aarch64` asset + its own recorded sha256.
- These pins make the *source build* reproducible. For end-to-end reproducibility
  you can also publish the built image digests (`hermes-agent`, `okengine-mcp`,
  `okengine-reader`) alongside a release and have packs reference them by digest.
