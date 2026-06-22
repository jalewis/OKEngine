# Publication / release checklist

Repeatable steps before making the repo public or cutting a release, so it doesn't rely on
memory. Tracks the public-release issues (okengine#82–#89). Run from the repo root.

## 0. Decide (one-time, blocks the rest)
- [x] **Public host: GitHub** (#85, decided). Public home = `github.com/jalewis/okengine`
      (matches `jalewis/okpacks-library` + `jalewis/hermes-cron-plus`). CI is GitHub Actions;
      SECURITY.md uses GitHub private reporting (#86); README has the Actions CI badge (#87).
      Dev remote stays internal GitLab; publish = a no-history snapshot to GitHub
      (`scripts/publish-snapshot.sh`, see §4) — not a mirror of the GitLab history.

## 1. Framing & metadata
- [ ] **Framing scrubbed** (#81/#83): no "OKF-first / conformant implementation of Google's
      format / builds-and-maintains-an-OKF-vault" phrasing outside explicit compatibility
      contexts. Check: `grep -rinE "OKF formalizes|OKF-first|builds and maintains an OKF" --include="*.md" --include="*.yaml" --include="*.toml" .`
- [ ] **Versions aligned** (#82): `pyproject.toml`, `engine-manifest.yaml engine_release`, and
      `SECURITY.md` agree. (Currently `0.2.0`.)
- [ ] **README** (#87): status + license + python badges; add the CI badge once the host is set.
- [ ] **SECURITY.md** (#86): vulnerability-reporting path matches the chosen host.
- [ ] No uncommitted working-tree changes you didn't intend: `git status --short`.

## 2. Quality gates
- [ ] `make check` (lint + tests + scaffold) is green.
- [ ] `make audit` (pip-audit + bandit) — no high-severity / known-CVE findings (#54).
- [ ] `make coverage` reviewed; `make typecheck` reviewed (#56/#57 — baselines).
- [ ] `make docker-smoke` — reader + mcp images build (#55).
- [ ] `git diff --check` — no whitespace/conflict-marker errors.

## 3. Secrets (mandatory — over full history, #84/#89)
- [ ] `gitleaks detect --source . --redact` (or `trufflehog git file://$PWD --only-verified`).
      Manual tracked-file scans are NOT enough — history must be scanned. Install the tool if
      absent; this cannot be skipped before going public.

## 4. Publish — no-history snapshot (#89/#94)
The public GitHub repo is a **history-free snapshot of the committed tree**, not a mirror of the
GitLab repo (which carries internal history/context). Build it with the gated builder:
- [ ] `make publish-snapshot` (or `bash scripts/publish-snapshot.sh`). It refuses a dirty tree,
      runs `make check`, exports `git archive HEAD` (committed files only — no `.git`, no
      untracked/ignored files), then gitleaks- + pattern-scans the staged tree and aborts on any
      hit. It **never pushes** — it stages to a dir and prints the manual `git push` commands.
- [ ] Review the staged tree, then run the printed `git push --force origin main` to publish.
- [ ] Confirm `.gitignore` excludes generated/runtime artifacts (e.g. `config/cron-plus-jobs.json`,
      `.hermes-data/`, vault data) — `git archive` already omits them, but keep the list honest.
- [ ] After pushing: `gh repo view jalewis/okengine`, README badges render, doc links resolve.

## Notes
- CI (GitHub Actions) runs lint, tests, scaffold, audit, typecheck, coverage, docker-build. It
  activates on the chosen host (#85). No GitLab CI is shipped (it's disabled here).
- `config/cron-plus-jobs.json` is a generated, gitignored artifact — never commit it (a deployed
  copy carries domain/pack jobs and would leak domain content into the engine repo).
