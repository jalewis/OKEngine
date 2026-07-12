# Cold-start walkthrough — what a first-time deployer actually hits

A from-scratch deploy of OKEngine, followed literally against the published docs
(no prior knowledge), with every friction point logged. Read this alongside
[`INSTALL.md`](../INSTALL.md) and [`deploy-a-new-domain.md`](deploy-a-new-domain.md)
if your first deploy doesn't behave.

**Bottom line:** the documented path works. `git clone → framework pull → cd pack
&& deploy.sh → add a model key` brings up a healthy stack and the agent lanes run
end-to-end (ingest → typed pages through the enforced write path → brief). The
notes below are the rough edges, ordered by how likely they are to trip you.

## The happy path (what actually works)

```bash
git clone <engine-repo> okengine
python okengine/scripts/framework.py list                    # browse the catalog
python okengine/scripts/framework.py pull <pack> my-brain    # SIBLING dir, not inside okengine/
cd my-brain && bash ../okengine/scripts/deploy.sh            # build (once) + up + crons + verify
#   then: set ONE model key in my-brain/.env  (OPENROUTER_API_KEY for the shipped default)
```

- **Model, out of the box:** the shipped `config.yaml` defaults to a **free-tier
  OpenRouter model** — set `OPENROUTER_API_KEY` in `my-brain/.env` and the LLM
  crons work with no config editing. Point at a different provider or a local
  model by editing `.hermes-data/config.yaml` (`model.default` / `provider` /
  `base_url`); see [`model-selection.md`](model-selection.md).
- **When do daily briefs run?** One knob: `OKENGINE_BRIEF_HOUR` in `.env`
  (gateway-local TZ, default 7 = 07:00). Every reader-facing daily brief lane ships a
  `@morning[:MM]` schedule that expands to that hour at deploy, so all your briefs
  cluster in your morning — set it once, don't tune lanes individually. (Set `TZ` too,
  or the hour is UTC.)
- **Feeds ship empty by design** (herd-safe). Nothing is ingested until you copy
  entries from `feeds/feeds.opml.example` into `feeds/feeds.opml` and redeploy the
  crons. With empty feeds the deployment is nearly free.
- **The agent works cold.** On a fresh deploy the ingest lane connects the read +
  enforced-write MCP servers (~20 tools; see the MCP server for the exact set), calls your model, and writes schema-
  conformant pages (`type`, required fields, the `raw:` dedupe key, structured
  wikilinks). Verified end-to-end.

## Rough edges, ordered by likelihood

1. **Run `deploy.sh` from *inside* the pack dir.** It takes the pack from the
   current directory, not from `ENGINE_DIR`. `cd my-brain && bash
   ../okengine/scripts/deploy.sh` — running it from the engine dir fails with
   `no docker-compose.yml`. (This is what the docs show; the failure just isn't
   self-explaining if you improvise.)

2. **First model turn is slow on a small local model.** A local 27B model runs a
   single ingest turn in ~30–90s, so a first ingest of a few items can take
   several minutes and several model calls. This is model speed, not a hang —
   watch `/opt/data/logs/cron-plus/<lane>-<ts>.log` in the gateway to see turns
   progressing. A hosted/faster model turns this into seconds. If you're on the
   free-tier default, expect middling latency.

3. **Some suggested example feeds are intermittent.** A pack's
   `feeds.opml.example` is a *suggestion list*, and some sources (e.g. arXiv's
   category RSS) are time-windowed and can legitimately return **zero items** at
   the moment you fetch. A `feed-fetch` reporting `0 items` is often the source,
   not a bug — check the URL in a browser, or pick a higher-volume feed to prove
   the pipeline before tuning your source set.

4. **`iwe` for the gateway — now automatic (was a trap).** The `backlinks-refresh`
   cron shells out to `iwe`, which the gateway image doesn't ship. `ensure-runtime`
   (run by `deploy.sh`) now stages the pinned, sha-verified binary into the mounted
   runtime dir automatically — **no action needed on a normal deploy.** If you see
   a `deployment-validate` FAIL about a missing `/opt/data/iwe/bin/iwe` (e.g. an
   air-gapped host where the fetch couldn't run), stage it by hand from the
   `iwe-org/iwe` v0.3.2 release; until then the reader/cockpit fall back to their
   own in-container build, so "what links here" still works.

## After deploy — is it actually healthy?

`deploy.sh`'s final step runs `post_deploy_verify.sh` (reader/MCP reachability +
auth, the enforced write path, cron registration, index readiness). Re-run it any
time: `( cd my-brain && bash ../okengine/scripts/post_deploy_verify.sh )`. The
weekly `deployment-validate` lane then guards the running instance (pins vs
runtime stamp, the iwe dependency, alias shadows, ownership, auth). A green
`post_deploy_verify` plus one successful LLM cron run (visible in the gateway's
cron-plus logs, or as new pages under `wiki/`) means you're live.
