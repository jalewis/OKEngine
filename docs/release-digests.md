# Deploying the images CI tested

Status: requires okengine#1048 phases 2–3 (the tested-digest verifier, the CI release lanes and
`deploy.sh --release-digests`).

A plain `deploy.sh` builds every engine image on the deployment host from the engine checkout.
That host build has the same source as the images the release pipeline tested, but it is a
different artifact. Compose stamps each deployment's project name into the image, and apt, pip and
npm resolve at build time, so the image the host runs never ran in CI.
`deploy.sh --release-digests` deploys the tested images themselves, by digest, and builds nothing.

## What the release pipeline records

For the commit a release tags, the main pipeline:

- builds every product image once from the candidate wheel: reader, okengine-mcp, cockpit,
  review-write, projection, operation-runner, and the gateway (hermes-agent, built by the same
  `scripts/build-engine-image.sh` a host would run);
- boots or exercises each one: the smoke, e2e, resilience and performance lanes run the sibling
  images, and the projection, operation-runner and gateway images each boot once with no network
  and no model;
- writes `tested-digests.json`, which names every image by its pushed manifest digest and its
  config digest (image ID), plus a `missing` list of any product image without a passing record.

Tagging the release (`vX.Y.Z`) runs one job, `release-promote`, which:

- tags each tested image `release/<service>:vX.Y.Z` by digest in the project registry;
- stores the list in the project's generic package registry as
  `okengine-release/vX.Y.Z/tested-digests.json`;
- links that list from the release.

It refuses a list for another commit, or one with anything `missing`.

## Deploying a release

1. Check out the release tag. Use a clone made after the tag existed, never a branch tip.
2. Fetch the release's list. The deploy copies it into the deployment, so the operator's copy may
   be deleted afterward; keep one anyway, outside `/tmp`, for the next rollback:

   ```bash
   mkdir -p ~/engines/releases/vX.Y.Z
   glab api "projects/<id>/packages/generic/okengine-release/vX.Y.Z/tested-digests.json" \
       > ~/engines/releases/vX.Y.Z/tested-digests.json
   ```

3. Deploy from the pack directory:

   ```bash
   bash "$ENGINE_DIR/scripts/deploy.sh" --release-digests ~/engines/releases/vX.Y.Z/tested-digests.json
   ```

`--release-digests` implies `--skip-build` and cannot be combined with `--rebuild` or a preselected
`OKENGINE_GATEWAY_IMAGE`.

Before it writes any pin (the pack `.env` or `docker-compose.okengine-image.yml`), it runs a
preflight (`release_load.py --preflight`). The preflight checks the list, the checkout, the required
images and the credentials. It then proves that the loader can reach the registry: a `crane digest`
through the loader, once per registry, must return the manifest digest the list pins. Images
already present locally by config ID need no registry, so a rollback onto local images does not
depend on it. A failed preflight leaves `.env` and the override untouched and deploys nothing.

Before any container is touched, it:

- refuses a list whose `commit_sha` is not the engine checkout's `HEAD`, or a checkout with
  uncommitted changes;
- refuses a list that has no tested image for a service the deployment runs;
- loads each image by digest through a digest-pinned loader container (`crane`). The loader only
  fetches and streams an archive to the host's `docker load`. It gets no Docker socket and no
  mounts. An image already present locally with the tested config ID is not fetched again;
- resolves the registry host name on the deploy host, where `getaddrinfo` honors `/etc/hosts`,
  and passes it to the loader as `--add-host <name>:<address>` (an IPv4 address when there is
  one). The loader container does not see the host's `/etc/hosts`, so a registry name recorded
  only there would otherwise fail with `no such host`. An IP-literal registry needs no mapping.
  A name the deploy host cannot resolve is a FAIL;
- verifies that every loaded image's config ID equals the list's `id`, for all images, before
  moving any local tag. A mismatch FAILs and nothing is tagged;
- tags the verified images with the references the deployment pins: the scoped engine tag, and
  the gateway image as `hermes-agent:okengine-<release>-<sha>.<project>.tested`, where
  `okengine-<release>-<sha>.<project>` is the deployment's scoped engine image tag and `<project>`
  its compose project. A separate gateway tag leaves any host-built image other deployments run
  untouched. The project scope means two packs rolled from two lists for the same commit (for
  example a push pipeline and a tag pipeline, which record different gateway IDs) never move each
  other's gateway tag.

The loader verifies a copy of the list staged in the deployment. On success the copy becomes
`<pack>/.okengine/generated/tested-digests.json`. The `.okengine/generated/` directory is
git-ignored and deploy-generated. `.env` then persists `OKENGINE_TESTED_DIGESTS=<that path>`.
A failed load keeps the copy and the pins from the previous roll. Deleting the operator's copy, or
pruning the deploy clone it sat in, does not affect later verification. The deploy then runs
`docker compose up -d --no-build`.

Backups: the copy under `.okengine/generated/` is archived with the deployment.
`OKENGINE_TESTED_DIGESTS` is a host-local `.env` key, so a backup without secrets does not carry
it, and the restored deployment reports the check as UNDETECTABLE until the next `deploy.sh`. That
deploy writes the key again (`--release-digests`) or removes it (a host build). A backup made with
`--include-secrets` carries `.env` whole. Its key names the copy by absolute path, so it still
resolves when the deployment is restored to the same directory; after a restore anywhere else,
redeploy.

## Registry credentials

The loader reads registry credentials from a host-local file. The file is never in the engine
checkout or the deployment, and its values never appear on any command line:

| Setting | Default | Contents |
|---|---|---|
| `OKENGINE_REGISTRY_ENV_FILE` (environment) | `~/.config/okengine/registry.env` | `OKENGINE_REGISTRY_USER=<deploy token username>` and `OKENGINE_REGISTRY_TOKEN=<deploy token>` |

Create the file with mode `0600`; the deploy refuses a file readable by group or others. Use a
project deploy token with only the `read_registry` scope. The file reaches the loader through
`docker run --env-file`, and the token reaches `crane auth login` on stdin.

## Verification

`post_deploy_verify.sh` (deploy step 6) runs `scripts/tested_digests.py` against the persisted
list:

- **PASS**: every running engine container (the six compose-built images and the gateway) runs a
  tested image, matched by its config ID or registry digest.
- **FAIL**: a running engine image is not in the list, or the list is unreadable or malformed.
  After `--release-digests`, a missing list is also a FAIL.
- **WARN (UNDETECTABLE)**: a deployment built on the host has no list. This is not a pass. A host
  build removes `OKENGINE_TESTED_DIGESTS` from `.env`, because its images are not the tested ones.

## Rolling back

To roll back, deploy the previous release's tested digests the same way:

1. check out the previous release tag;
2. fetch that release's `tested-digests.json`;
3. run `deploy.sh --release-digests` with it.

Nothing is rebuilt. The previous images are usually still in the local store; otherwise they load
from the registry by digest. That is why the registry keeps `release/<service>:vX.Y.Z` tags.
`post_deploy_verify.sh` then proves the rollback landed on tested digests.
