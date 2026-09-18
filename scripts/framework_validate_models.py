"""Cron schedule and model-profile validation services."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.framework_validate_report import Report


class ModelChecks:
    def __init__(self, *, model_profiles):
        self.model_profiles = model_profiles

    def _cron_expr(self, d: dict) -> str:
        """Resolve a cron def's schedule expression across the supported shapes:
        {"schedule": {"expr": "..."}} | {"schedule": "..."} | {"expr": "..."}.
        Returns "" when no usable expression is present."""
        sched = d.get("schedule")
        if isinstance(sched, dict):
            return str(sched.get("expr") or "").strip()
        if isinstance(sched, str):
            return sched.strip()
        return str(d.get("expr") or "").strip()

    def _fixed_cron_hours(self, expr: str) -> list[int]:
        """Expand fixed/list/range/step hour fields; bare every-hour is not fixed."""
        match = re.match(r"^\S+\s+(\S+)\s", str(expr or ""))
        if not match:
            return []
        hours: set[int] = set()
        for token in match.group(1).split(","):
            token = token.strip()
            if token == "*":
                continue
            step, base = 1, token
            if "/" in token:
                base, _, raw_step = token.partition("/")
                if not (raw_step.isdigit() and int(raw_step) >= 1):
                    continue
                step = int(raw_step)
            if base == "*":
                low, high = 0, 23
            elif "-" in base:
                raw_low, _, raw_high = base.partition("-")
                if not (raw_low.isdigit() and raw_high.isdigit()):
                    continue
                low, high = int(raw_low), int(raw_high)
            elif base.isdigit():
                low = high = int(base)
            else:
                continue
            hours.update(range(low, high + 1, step))
        return sorted(hour for hour in hours if 0 <= hour <= 23)

    def _dst_schedule_problem(self, expr: str) -> str | None:
        """Return the spring-forward loss hazard for a fixed-hour cron, if any."""
        hours = self._fixed_cron_hours(expr)
        if 2 not in hours:
            return None
        if 3 in hours:
            return "02:xx and 03:xx collapse to one run during DST spring-forward"
        if len(hours) <= 4:
            return "low-frequency 02:xx run is skipped during DST spring-forward"
        return None

    def check_model_profiles(self, pack: Path, r: Report) -> None:
        """Validate the optional model-profiles registry (okengine#151) and that every `@<profile>`
        reference the operator wrote (pack domain crons + extension-models.json) resolves — fail
        BEFORE deploy, where an undefined reference would otherwise abort the fold."""
        mp = self.model_profiles()
        f = pack / ".okengine" / "model-profiles.yaml"
        try:
            profiles = mp.load_profiles(pack)
        except Exception as e:  # malformed YAML / wrong shape
            r.fail(".okengine/model-profiles.yaml", str(e)[:140])
            return
        if not f.is_file():
            # No registry is fine — but an `@`-ref with no registry can never resolve, so flag it.
            refs = self._collect_model_refs(pack, mp)
            if refs:
                r.fail(
                    "model profiles",
                    f"{sorted(refs)} referenced but .okengine/model-profiles.yaml "
                    "is absent — define the profiles or use literal model names",
                )
            else:
                r.info(
                    "model profiles",
                    "none declared (lanes use literal models / the config default)",
                )
            return
        shape_errs = mp.validate_profiles(profiles)
        if shape_errs:
            for e in shape_errs:
                r.fail("model profiles", e)
            return
        refs = self._collect_model_refs(pack, mp)
        missing = sorted(n for n in refs if n not in profiles)
        if missing:
            r.fail(
                "model profiles",
                f"undefined profile reference(s): {missing} (defined: {sorted(profiles)})",
            )
        else:
            r.ok("model profiles", f"{len(profiles)} profile(s); {len(refs)} reference(s) resolve")

    def _collect_model_refs(self, pack: Path, mp) -> set[str]:
        """Profile names referenced (`@name`) across the pack's operator-facing model hooks: domain
        cron defs and the extension-models override map."""
        refs: set[str] = set()
        dc = pack / "crons" / "domain-crons.json"
        if dc.is_file():
            try:
                for d in json.loads(dc.read_text(encoding="utf-8")) or []:
                    if isinstance(d, dict) and mp.is_ref(d.get("model")):
                        refs.add(mp.ref_name(d["model"]))
            except (ValueError, OSError):
                pass  # shape errors reported by check_crons
        em = pack / ".okengine" / "extension-models.json"
        if em.is_file():
            try:
                for v in (json.loads(em.read_text(encoding="utf-8")) or {}).values():
                    if mp.is_ref(v):
                        refs.add(mp.ref_name(v))
            except (ValueError, OSError):
                pass
        return refs
