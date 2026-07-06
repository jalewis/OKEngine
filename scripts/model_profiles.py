#!/usr/bin/env python3
"""Named model profiles — deploy-time resolution of `@<profile>` model references (okengine#151).

A cron lane addresses a model by a single string field (`model`). That is the *only* model
hook an extension lane carries (`extension_compose` forwards `op.model` as a string), so to let
a lane switch ollama HOST or context length — not just model name — we resolve a profile
reference at DEPLOY time.

A profile is a fully-specified endpoint:

    # <pack>/.okengine/model-profiles.yaml   (operator/deployment tier, like extension-prompts.json)
    profiles:
      reasoning: {provider: custom, base_url: http://host-a:11436/v1, model: qwen3.5:27b, ollama_num_ctx: 65536}
      bulk:      {provider: custom, base_url: http://host-b:11436/v1, model: qwen3.5:9b,  ollama_num_ctx: 65536}
      light:     {provider: custom, base_url: http://host-b:11436/v1, model: qwen3.5:4b,  ollama_num_ctx: 32768}

A lane opts in with an `@`-sigil reference, e.g. `model: "@reasoning"`. At deploy the reference
is expanded into the concrete `model` / `provider` / `base_url` (+ `ollama_num_ctx`) fields the
cron-plus scheduler forwards to Hermes' `run_job()`.

The `@` sigil disambiguates intent from a literal model name: a *bare* string (`qwen3.5:9b`,
`openai/gpt-oss-120b:free`) is a literal model — passed through UNCHANGED (backward compatible).
An `@name` that no profile defines is a fail-loud error (a typo'd/stale reference must not
silently fall back to the default model).

Expansion runs on the DEPLOY copy only (alongside the `@jitter` expansion in
`deploy-cron-plus-jobs.sh`), so the generated `config/cron-plus-jobs.json` keeps `@`-refs and
stays round-trippable (`cron_pack_split.py check`).

`ollama_num_ctx` is carried onto the job dict here, but is only HONORED per-lane once the
companion Hermes patch (okengine#151 / Suggestion 2b) forwards it through `run_job()`. Until
then it is an inert, forward-compatible field on the job.
"""
from __future__ import annotations

from pathlib import Path

# Fields a profile may set on a job. `model` is required; the rest are optional.
PROFILE_FIELDS = ("provider", "base_url", "model", "ollama_num_ctx")
SIGIL = "@"


def is_ref(model) -> bool:
    """True if a job's `model` value is a profile reference (`@name`) rather than a literal."""
    return isinstance(model, str) and model.startswith(SIGIL) and len(model) > 1


def ref_name(model: str) -> str:
    return model[len(SIGIL):]


def load_profiles(pack_dir) -> dict:
    """Load `<pack>/.okengine/model-profiles.yaml` -> ``{name: spec}``. ``{}`` when the file is
    absent (zero-impact until an operator defines profiles)."""
    import yaml
    f = Path(pack_dir) / ".okengine" / "model-profiles.yaml"
    if not f.is_file():
        return {}
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("model-profiles.yaml must be a mapping with a top-level `profiles:` key")
    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("model-profiles.yaml `profiles:` must be a {name: spec} map")
    return profiles


def load_lane_models(pack_dir) -> dict:
    """Load `<pack>/.okengine/cron-models.json` -> ``{job_name: model}``. A deploy-time per-lane
    model override for ANY cron lane (engine / engine-template / domain) — the counterpart to
    extension-models.json (which covers only extension lanes, applied at compose). ``{}`` when the
    file is absent. The model value is usually an `@profile` ref, resolved by expand_jobs after."""
    import json
    f = Path(pack_dir) / ".okengine" / "cron-models.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise ValueError(f"cron-models.json: {e}")
    if not isinstance(data, dict):
        raise ValueError("cron-models.json must be a {job_name: model} map")
    return data


def apply_lane_models(jobs: list[dict], overrides: dict) -> tuple[int, list[str]]:
    """Set ``model`` on each named lane from the override map, IN PLACE, BEFORE @-profile expansion.
    Fail-loud on a key matching no job (a stale/typo'd lane name must not pass silently) or a
    non-string model. Returns ``(n_applied, errors)``; a non-empty error list means do not deploy."""
    errors: list[str] = []
    by_name = {j.get("name"): j for j in jobs}
    n = 0
    for name, model in overrides.items():
        if name not in by_name:
            errors.append(f"cron-models.json: no cron lane named {name!r}")
        elif not isinstance(model, str) or not model.strip():
            errors.append(f"cron-models.json[{name!r}]: model must be a non-empty string")
        else:
            by_name[name]["model"] = model
            n += 1
    return n, errors


def validate_profiles(profiles: dict) -> list[str]:
    """Shape-check the profile registry. Returns a list of human-readable errors (empty == ok)."""
    errors: list[str] = []
    for name, spec in profiles.items():
        if not isinstance(spec, dict):
            errors.append(f"model profile {name!r}: must be a mapping, got {type(spec).__name__}")
            continue
        if not spec.get("model"):
            errors.append(f"model profile {name!r}: missing required 'model'")
        if spec.get("provider") == "custom" and not spec.get("base_url"):
            errors.append(f"model profile {name!r}: provider 'custom' requires 'base_url'")
        unknown = [k for k in spec if k not in PROFILE_FIELDS]
        if unknown:
            errors.append(f"model profile {name!r}: unknown field(s) {unknown} "
                          f"(allowed: {list(PROFILE_FIELDS)})")
    return errors


def unresolved_refs(jobs: list[dict], profiles: dict) -> list[str]:
    """Job names whose `@`-reference names a profile not in the registry — fail-loud keys.
    Returns formatted error strings (empty == every reference resolves)."""
    errors: list[str] = []
    for j in jobs:
        m = j.get("model")
        if is_ref(m) and ref_name(m) not in profiles:
            errors.append(f"job {j.get('name')!r}: references model profile "
                          f"{ref_name(m)!r} which is not defined in model-profiles.yaml")
    return errors


def expand_jobs(jobs: list[dict], profiles: dict) -> tuple[int, list[str]]:
    """Resolve each job's `@`-reference in place into the concrete profile fields.

    Returns ``(n_expanded, errors)``. A bare (non-`@`) model is left untouched. An `@`-ref to
    an undefined profile is an error and the job is left unchanged (do not deploy)."""
    n = 0
    errors = unresolved_refs(jobs, profiles)
    bad = {e.split("'")[1] for e in errors}  # job names that failed, to skip below
    for j in jobs:
        m = j.get("model")
        if not is_ref(m) or j.get("name") in bad:
            continue
        spec = profiles[ref_name(m)]
        for f in PROFILE_FIELDS:
            if spec.get(f) is not None:
                j[f] = spec[f]
        n += 1
    return n, errors
