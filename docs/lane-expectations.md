# Lane outcome expectations (`expect:`)

Every lane (an engine cron, an extension operation, or a pack domain cron) declares what
"performing as desired" means for it. A lane that runs on schedule and produces nothing can then
be told apart from one that works, without adding an alert: the fleet-health dashboard evaluates
each expectation against the artifacts the lane actually produced (okengine#889).

`output_contract` and `expect:` answer different questions:

| | `output_contract` ([model-write-contracts.md](model-write-contracts.md)) | `expect:` (this page) |
|---|---|---|
| question | what **may** this lane write? | what **should** this lane produce? |
| enforced | at every write, by the write path | at `framework validate` (shape), and on the dashboard (outcome) |
| catches | a model writing outside its lane | a lane that quietly stopped producing |

A lane's `expect:` must be achievable under its contract: a `produces` namespace outside the
lane's `allowed_namespaces` fails validation, because it could never be met.

## Why this exists

On one live vault in September 2026, lanes kept running while their output stopped, and the
dashboard, which judged lanes by whether they *ran*, showed most of them healthy:

| Lane | What the dashboard showed | What was actually happening |
|---|---|---|
| lacuna | 🟢 ran 19h ago | the gate skipped 78 of 81 runs; 2 pages ever |
| trends-refresh | 🔴 runner error | the gate skipped every weekly run for two months |
| forecast-review | 🔴 errored once | reviews scattered across three namespaces for months |
| raw-compile | 🟢 ran 0h ago | no-op re-offers ate most of each run; the backlog stalled |

With `expect:`, the first two read "not producing since …" the week they stopped.

## The shapes

Declare one or more under `expect:`. Windows are `<n>h`, `<n>d`, `<n>w`, or `<n>x` (n × the
lane's own schedule cadence, so the expectation follows a schedule change).

### `produces`

The lane owes new or updated pages.

| key | required | meaning |
|---|---|---|
| `namespace` | one of `namespace` / `type` | wiki namespace whose pages count (must be in the lane's `output_contract`) |
| `type` | one of `namespace` / `type` | page `type` that counts |
| `at_least` | yes | minimum number of pages … |
| `per` | yes | … within this window |

```json
"expect": {"produces": {"namespace": "trends", "at_least": 1, "per": "4w"}}
```

### `drains`

The lane works a queue: its backlog must shrink, or already be empty.

| key | required | meaning |
|---|---|---|
| `max_stalled_runs` | no | runs a non-empty backlog may stay flat before the lane reads "under" |

```json
"expect": {"drains": {"max_stalled_runs": 6}}
```

### `maintains`

The lane keeps an artifact fresh (a dashboard, an index, a feed capture directory).

| key | required | meaning |
|---|---|---|
| `artifact` | yes | vault-relative path: a file, or a directory judged by its newest file |
| `max_age` | yes | the artifact is stale beyond this age |

```json
"expect": {"maintains": {"artifact": "wiki/dashboards/fleet-health.md", "max_age": "3x"}}
```

### `may_idle`

A wake-gated lane may legitimately skip ("nothing to do") — but not forever.

| key | required | meaning |
|---|---|---|
| `max_idle_runs` | yes | consecutive gate skips before the lane reads "under" |

Combine it with the shape that describes the lane's output when it does wake:

```json
"expect": {"produces": {"namespace": "lacuna", "at_least": 1, "per": "8x"},
           "may_idle": {"max_idle_runs": 8}}
```

## Where it goes

| lane kind | file | key |
|---|---|---|
| engine cron | `config/engine-crons.json` | `"expect"` on the job |
| extension operation | `extensions/<id>/extension.yaml` (or `crons/<op>.cron.json`) | `expect:` on the operation |
| pack domain cron | `<pack>/crons/domain-crons.json` | `"expect"` on the job |

`cron_pack_split.py` and extension composition carry `expect` into the deployed
`cron-plus-jobs.json`, where the dashboard reads it.

## Validation

- **Engine lanes and first-party extension operations** must declare `expect:` or be listed in
  `config/expect-allowlist.yaml` with a reason. The allowlist **only shrinks**: when a listed
  lane gains `expect:`, delete its entry and lower `max_entries`; an entry for a lane that no
  longer exists is an error; and `tests/test_lane_expect.py` fails if the list or its cap grows
  relative to the merge base. A new lane never goes on it.
- **Pack lanes** (domain crons, pack and operator extensions): `framework validate <pack>` FAILs
  a malformed `expect:` and WARNs when it is missing ("undeclared"). Undeclared pack lanes will
  FAIL in a future release, once packs have migrated (okengine#891).
- A structurally invalid `expect:` (unknown shape or key, a bad window, a `produces` namespace
  outside the contract) FAILs everywhere, allowlisted or not.

The failure names the lane, links this page, and prints a starting point matched to the lane:
a model lane gets `produces` in its first contract namespace; a script lane gets `maintains`.

## Reading the result

The fleet-health dashboard's **Outcome** column (okengine#892) evaluates each lane: *meeting*,
*under*, *not producing since \<date\>*, *never met*, or *undeclared (allowlisted)*. Missing
evidence reads *unknown*, never *not producing*. It adds no alert: the page is the signal, sorted
worst-first, with the lanes added in the last 30 days shown beside the ones that have gone
longest without meeting their expectation.
