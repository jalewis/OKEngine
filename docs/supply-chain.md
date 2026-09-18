# Supply chain & dependency pinning

OKEngine's build is pinned so a given engine release builds reproducibly. This
documents what is pinned, where, and how to bump each dependency safely.

## What's pinned

| Dependency | Where | Pin | Integrity |
|---|---|---|---|
| **Base image** (`python:3.13-slim-trixie`) | all service Dockerfiles and the release fault-gateway Dockerfile | public digest `sha256:c33f0bc4…105e4f`; CI mirror digest `sha256:470a5fa0…dd22` | digest pin (also pins the apt snapshot) |
| **qmd** (npm) | `okengine-mcp/Dockerfile` | `@tobilu/qmd@2.5.3` | npm version pin |
| **node-gyp** (npm) | `okengine-mcp/Dockerfile` | `node-gyp@11` | npm version pin |
| **MCP Python deps** | `okengine-mcp/requirements.txt` | `==` pins (`mcp`, `PyYAML`, `uvicorn`) | exact versions |
| **Reader Python deps** | `okengine-reader/requirements.txt` | `==` pins | exact versions |
| **Hermes-Agent** runtime | `engine-manifest.yaml` → `runtime.pinned_sha` | tag `v2026.9.14` → commit `345cd2b057a452236de401d3534b8502a7465e8d` | `build-engine-image.sh` verifies the clone matches |
| **cron-plus** plugin (required runtime scheduler) | `engine-manifest.yaml` → `dependencies.cron-plus`, `INSTALL.md` §4 | commit `bdb6cf5f…c836d` (untagged) | operator clones + `git checkout` the pin |

apt packages are intentionally **not** version-pinned individually: the base-image
digest pins the Debian snapshot they come from, so they're reproducible without the
brittleness of per-package version pins that disappear from the mirrors.

## Updating a dependency

Bump deliberately, one at a time, and rebuild + test (`make check` + a real image
build) before committing.

### Base image (digest)
The tag (`python:3.13-slim-trixie`) moves when upstream rebuilds (e.g. for CVE
fixes). To adopt a new snapshot, re-resolve the digest and update every
`PYTHON_BASE_IMAGE` default:
```sh
TOKEN=$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -fsS -I -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/vnd.oci.image.index.v1+json" \
  https://registry-1.docker.io/v2/library/python/manifests/3.13-slim-trixie | grep -i docker-content-digest
```

Release CI overrides that public default with the same platform image stored in
the project registry, which serves it from its own storage. This keeps a fresh
DinD daemon from depending on Docker Hub's blob CDN at all. That CDN broke CI
repeatedly (#556); the cause was long recorded here as CloudFront being
"IPv6-only", which was wrong — a DNS filter was withholding its A record by
blocking `cloudfront.net` (#751). After changing the public digest:

1. Import the selected `linux/amd64` image into
   `$CI_REGISTRY_IMAGE/base/python` under a digest-named tag.
2. Read the destination manifest digest from the registry; do not assume it is
   the multi-platform Docker Hub index digest.
3. Update `.release-stack.variables.OKENGINE_PYTHON_BASE_IMAGE` to the internal
   tag **and destination digest**.
4. Run all four release jobs. A normal deployment still uses the public default.

This installation's registry is HTTP-only and its hostname is supplied to the
runner host through `/etc/hosts`. The unprotected, non-secret project variable
`OKENGINE_CI_REGISTRY_HOST_IP` carries that installation address into the DinD
service. Other installations with working registry DNS may omit it.

### Node base image (digest)
`okengine-mcp` and `okengine-cockpit` copy Node from the official `node` image
rather than installing Debian's `nodejs npm`. Debian packages each of npm's
dependencies as a separate `.deb`, so that one line pulled 340 `node-*`
packages and a Node (v20) older than qmd supports. Both Dockerfiles declare
`NODE_BASE_IMAGE` as a **linux/amd64 platform** digest, and they must agree
(`tests/test_dockerfile_pins.py`).

To bump it, resolve the platform digest and update both Dockerfiles, the smoke
compose default, and `.release-stack.variables.OKENGINE_NODE_BASE_IMAGE`:
```sh
crane digest --platform linux/amd64 node:22-slim
```
Then copy it into the project registry **with `crane copy`**, not `docker
pull`/`push`. `crane copy` preserves the manifest byte-for-byte, so the
destination digest equals Docker Hub's own digest and the mirror can be checked
against upstream. `docker pull`/`push` re-serializes the manifest, which is why
the Python mirror above has a destination digest that differs from its source
and must be read back from the registry instead.

Node is a **runtime** dependency of both images (qmd, marp), not just a build
tool. It links `libstdc++`: `okengine-mcp` gets that from `build-essential`,
`okengine-cockpit` transitively from `chromium`. Prove a bump by running qmd
and by rendering a marp deck to PDF as the non-root user — not by checking a
version string.

### qmd / node-gyp
```sh
npm view @tobilu/qmd version        # latest
```
Pin the chosen version in `okengine-mcp/Dockerfile`, and confirm its
`engines.node` range against the Node base image above — the Debian-Node build
installed an unsupported version and only logged an `EBADENGINE` warning.

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
- These pins make the *source build* reproducible. For end-to-end reproducibility
  you can also publish the built image digests (`hermes-agent`, `okengine-mcp`,
  `okengine-reader`) alongside a release and have packs reference them by digest.
