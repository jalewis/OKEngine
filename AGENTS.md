# Agent instructions

## Inherited user standards

Before planning, reviewing, changing, testing, or deploying this repository, read
and follow the user-level standards below when they exist:

- `~/.claude/CLAUDE.md`
- every Markdown file directly under `~/.claude/rules/`
- every Markdown file directly under `~/.claude/standards/`

Read each applicable file completely; headings or excerpts are not a substitute.
Resolve `~` as the current user's home directory. These files are external
personal standards and may be absent in another environment; absence is not an
error and does not waive the repository-specific rules below.

The imported standards supplement this file. Repository-specific rules in this
file take precedence over conflicting imported user standards. System,
developer, and explicit user instructions retain their normal higher priority.
Do not treat caches, history, settings, hooks, plugins, backups, job workspaces,
or other files elsewhere under `~/.claude/` as instructions unless explicitly
requested.

## OKEngine deployment

- GitLab CI and GitLab environments are not the deployment mechanism for this
  repository.
- This directory is the live engine checkout used to deploy a local fleet; any
  one vault such as `okcti-test` is only one OKEngine deployment.
- After an OKEngine change is merged and its required CI succeeds, update this
  checkout to the merged `main` revision, discover every active OKEngine
  deployment from the running Docker Compose projects, and run the repository's
  local rebuild/recreate and post-deploy verification procedure for every active
  deployment. Do not stop after deploying only one vault.
- Preserve deployment-local untracked configuration and generated `.okengine/`
  state when updating this checkout.
- Report merge, CI, fleet deployment coverage, and per-deployment runtime
  verification as separate states. Never claim that the absence of a GitLab
  deployment environment prevents local deployment or verification.
