"""Template, application-profile, and policy-plane validation services."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def check_tokens(pack: Path, report, *, token_scan, token_re) -> None:
    hits: list[str] = []
    for relative in token_scan:
        path = pack / relative
        if not path.is_file():
            continue
        tokens = sorted(set(token_re.findall(path.read_text(encoding="utf-8", errors="replace"))))
        if tokens:
            hits.append(f"{relative}: {', '.join(tokens[:6])}")
    if hits:
        report.fail("unrendered {{tokens}}", "; ".join(hits))
    else:
        report.ok("template tokens", "all rendered")


def check_application_profile(pack: Path, report, *, engine_root: Path, load_yaml) -> None:
    declaration = pack / ".okengine" / "application.yaml"
    if not declaration.is_file():
        return
    try:
        path = engine_root / "scripts" / "application_profiles.py"
        spec = importlib.util.spec_from_file_location("application_profiles", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        errors = module.validate(pack, engine_root)
    except Exception as exc:
        report.fail("application profile", f"validator failed: {exc}")
        return
    if errors:
        for error in errors:
            report.fail("application profile", error)
    else:
        data = load_yaml(declaration) or {}
        report.ok("application profile", f"{data.get('profile')} {data.get('profile_version')}")


def check_policy_plane(pack: Path, report, *, engine_root: Path, resolve_prompt=None) -> None:
    try:
        path = engine_root / "tools" / "policy_plane.py"
        spec = importlib.util.spec_from_file_location("okengine_policy_plane", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        policy = module.effective_policy(pack)
    except Exception as exc:
        report.fail("policy plane", str(exc))
        return
    report.ok("policy plane", f"{len(policy['rules'])} rule(s); digest {policy['digest'][:12]}")
    prompt_path = pack / "crons" / "engine-template-prompts.json"
    if not prompt_path.is_file():
        return
    try:
        prompts = json.loads(prompt_path.read_text(encoding="utf-8"))
        prompt_value = prompts.get("source-quality-backfill") or ""
        prompt = (
            resolve_prompt(pack, prompt_value)
            if resolve_prompt is not None
            else str(prompt_value.get("prompt") or "")
            if isinstance(prompt_value, dict)
            else str(prompt_value)
        )
        errors = module.check_prompt(policy, "cron:source-quality-backfill", prompt)
    except Exception as exc:
        errors = [str(exc)]
    for error in errors:
        report.fail("source-quality capability/prompt", error)
    if not errors:
        report.ok("source-quality capability/prompt", "prompt conforms to enforced field/body authority")
