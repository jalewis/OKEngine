# CI pipeline watcher

`ci/pipeline_watch.py` closes the monitoring gap tracked by #611. It runs from a user systemd
timer on an operator host, outside the GitLab runner it observes.

Every five minutes it checks the newest finished default-branch and scheduled pipelines. A red
surface produces a durable, assigned GitLab issue. The issue names
the failed lanes, their consecutive failure count and duration, and the merge or commit associated
with a newly red default branch. It is updated while red and closed on recovery.

The same poll inspects traces for running `mutation-critical` and `mutation-full` jobs. A job is
reported when its newest `mutation-progress` record is older than 120 seconds (configurable with
`--mutation-heartbeat-max-age-seconds`) or when it has run that long without emitting any progress
record. This is a live-job failure signal: it does not wait for the pipeline to finish or infer
progress from GitLab's `running` status.

The poll also downloads retained mutation aggregate summaries into
`%h/.local/state/okengine/mutation-history/` and applies the #612 graduation rule. Any critical
target without a score in three consecutive published runs is named in the durable alert issue.
This check stays on the operator host: a broken runner cannot suppress the detector that watches
it. Aggregate jobs are preferred over shard artifacts so each history entry represents one whole
campaign and one exact manifest revision.

Install the tracked units after merging:

```bash
systemctl --user link "$PWD/deploy/systemd/okengine-ci-watch.service"
systemctl --user link "$PWD/deploy/systemd/okengine-ci-watch.timer"
systemctl --user link "$PWD/deploy/systemd/okengine-ci-watch-failure.service"
systemctl --user link "$PWD/deploy/systemd/okengine-ci-watch-liveness.service"
systemctl --user link "$PWD/deploy/systemd/okengine-ci-watch-liveness.timer"
systemctl --user enable --now okengine-ci-watch.timer okengine-ci-watch-liveness.timer
```

To detect partial runner-capacity loss, configure every GitLab runner entry that shares the
same executor pool and its host-wide capacity. The service reads this optional file:

```ini
# ~/.config/okengine/ci-watch.env
OKENGINE_CI_RUNNER_IDS=15,16
OKENGINE_CI_RUNNER_CAPACITY=5
```

The watcher alerts only when a project job has remained pending for more than two minutes while
the combined active jobs on those runners are below capacity. The alert names runner and manager
IDs, active/expected capacity, the oldest queued job and its pipeline. Leaving the variables unset
disables this host-specific check; pipeline and mutation checks continue normally.

The liveness timer is intentionally separate. It fails and writes to the user journal if the primary
watcher has not completed a successful GitLab query in 15 minutes. Watcher process failures are also
recorded in the user journal. Verify the stale-heartbeat path with a temporary state path rather than corrupting
the production heartbeat:

```bash
python ci/pipeline_watch.py --check-liveness --state /tmp/absent-ci-watch-state.json
systemctl --user list-timers okengine-ci-watch.timer okengine-ci-watch-liveness.timer
```

The state file is `%h/.local/state/okengine/ci-watch.json`. A healthy run exits 0, an observed red
pipeline exits 1 (declared successful by the primary systemd unit), and an API/tooling failure exits
2 without advancing the heartbeat. Keeping tooling failures distinct from exit 1 ensures systemd
records them as failed checks immediately and invokes `okengine-ci-watch-failure.service`. The
separate liveness timer invokes the same failure notifier when the heartbeat remains stale.
