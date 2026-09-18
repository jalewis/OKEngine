#!/usr/bin/env python3
"""Execute the production-like 100k deployment qualification plan."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
for _IMPORT_ROOT in (ENGINE, ENGINE / "src"):
    # Candidate sources must precede any older wheel in the operator's venv.
    sys.path.insert(0, str(_IMPORT_ROOT))


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("required measure has zero samples")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction + .999) - 1))]


def summarize(samples: list[float]) -> dict:
    return {"samples": len(samples), "p50_seconds": percentile(samples, .5),
            "p95_seconds": percentile(samples, .95), "max_seconds": max(samples),
            "raw_seconds": samples}


def memory_bytes(output: str) -> int:
    units = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
    values = []
    for line in output.splitlines():
        match = re.search(r'"MemUsage"\s*:\s*"([0-9.]+)([KMG]?i?B)\s*/', line)
        if match and match.group(2) in units:
            values.append(int(float(match.group(1)) * units[match.group(2)]))
    if not values:
        raise RuntimeError("steady-state memory command produced no parseable MemUsage samples")
    # ``docker compose stats`` emits one record per service. Qualification is for
    # the integrated deployment, so report its aggregate resident footprint;
    # taking only the largest container can hide unbounded fleet-wide growth.
    return sum(values)


def enforce_budgets(report: dict, budgets: dict) -> None:
    errors = []
    for name, limit in (budgets.get("phase_p95_seconds") or {}).items():
        actual = (report["phases"].get(name) or {}).get("p95_seconds")
        if actual is None or actual > float(limit):
            errors.append(f"phase {name} p95 {actual} exceeds {limit}")
    for name, limit in (budgets.get("endpoint_p95_seconds") or {}).items():
        actual = (report["endpoints"].get(name) or {}).get("p95_seconds")
        if actual is None or actual > float(limit):
            errors.append(f"endpoint {name} p95 {actual} exceeds {limit}")
    memory_limit = budgets.get("steady_state_memory_bytes")
    memory = report["phases"].get("steady_state_memory", {}).get("max_memory_bytes")
    if memory_limit is not None and (memory is None or memory > int(memory_limit)):
        errors.append(f"steady-state memory {memory} exceeds {memory_limit}")
    disk_limit = budgets.get("disk_growth_bytes")
    growth = report["environment"]["disk_growth_bytes"]
    if disk_limit is not None and growth > int(disk_limit):
        errors.append(f"disk growth {growth} exceeds {disk_limit}")
    if errors:
        raise RuntimeError("qualification budget failure: " + "; ".join(errors))


def run_command(command: list[str], deployment: Path, timeout: int) -> dict:
    expanded = [part.replace("{deployment}", str(deployment)).replace("{engine}", str(ENGINE))
                for part in command]
    started = time.monotonic()
    environment = os.environ.copy()
    if expanded[:2] == ["docker", "compose"]:
        environment.setdefault(
            "COMPOSE_FILE",
            os.pathsep.join((str(deployment / "docker-compose.yml"),
                             str(ENGINE / "config/docker-compose.qualification.yml"))),
        )
    result = subprocess.run(expanded, cwd=deployment, capture_output=True, text=True,
                            timeout=timeout, env=environment)
    elapsed = time.monotonic() - started
    if result.returncode:
        diagnostic = (result.stderr + "\n" + result.stdout)[-4000:]
        raise RuntimeError(f"command failed ({result.returncode}): {expanded}: {diagnostic}")
    return {"seconds": elapsed, "command": expanded, "stdout": result.stdout[-4000:]}


def governed_write(deployment: Path, relative_path: str) -> dict:
    """Exercise the real corpus transaction path against a synthetic qualification page."""
    target = deployment / "wiki" / relative_path
    if not target.is_file():
        raise RuntimeError(f"governed-write target is missing: {relative_path}")
    previous = os.environ.get("WIKI_PATH")
    os.environ["WIKI_PATH"] = str(deployment)
    try:
        spec = importlib.util.spec_from_file_location(
            "qualification_write_server", ENGINE / "okengine-mcp/write_server.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load governed write service")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        started = time.monotonic()
        result = module._update(relative_path, {})
        elapsed = time.monotonic() - started
    finally:
        if previous is None:
            os.environ.pop("WIKI_PATH", None)
        else:
            os.environ["WIKI_PATH"] = previous
    if not re.match(r"^updated(?::|\s)", str(result)):
        raise RuntimeError(f"governed write failed: {result}")
    return {"seconds": elapsed, "operation": "update_entity", "path": relative_path,
            "result": result}


def run_overlap(item: dict, deployment: Path, timeout: int) -> dict:
    if item.get("operation") == "governed_write":
        return governed_write(deployment, str(item.get("path") or ""))
    if not isinstance(item.get("command"), list):
        raise RuntimeError(f"overlap item {item.get('name')} lacks command or governed operation")
    return run_command(item["command"], deployment, timeout)


def validated_http_url(value: object) -> str:
    """Reject local-file and custom-scheme probes before any network call."""
    url = str(value or "")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"qualification endpoint must be an HTTP(S) URL: {url!r}")
    return url


def sample_http(spec: dict) -> dict:
    url = validated_http_url(spec.get("url"))
    samples = []
    for _ in range(int(spec.get("samples") or 0)):
        started = time.monotonic()
        with urllib.request.urlopen(  # nosec B310
                url, timeout=spec.get("timeout", 30)) as response:
            body = response.read()
            if response.status != int(spec.get("status", 200)):
                raise RuntimeError(f"{spec['name']} returned HTTP {response.status}")
            marker = str(spec.get("contains") or "").encode()
            if marker and marker not in body:
                raise RuntimeError(f"{spec['name']} response lacks required marker")
        samples.append(time.monotonic() - started)
    return summarize(samples)


def wait_http(spec: dict, timeout: int) -> float:
    """Measure recovery until an endpoint is actually ready, not merely restarted."""
    url = validated_http_url(spec.get("url"))
    started = time.monotonic()
    deadline = started + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # nosec B310
                    url, timeout=min(5, timeout)) as response:
                body = response.read()
                marker = str(spec.get("contains") or "").encode()
                if response.status == int(spec.get("status", 200)) and (
                    not marker or marker in body
                ):
                    return time.monotonic() - started
        except OSError as exc:
            last_error = exc
        time.sleep(float(spec.get("interval_seconds") or 1))
    raise RuntimeError(f"{spec['url']} did not become ready within {timeout}s: {last_error}")


def sample_mcp(spec: dict) -> dict:
    async def one() -> float:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        started = time.monotonic()
        async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {spec['token']}"},
                timeout=spec.get("timeout", 60)) as client:
            async with streamable_http_client(
                    spec["url"], http_client=client) as (read, write, _session):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(spec["tool"], spec.get("arguments") or {})
                    if result.isError:
                        raise RuntimeError(f"{spec['name']} MCP tool returned an error")
        return time.monotonic() - started

    samples = [asyncio.run(one()) for _ in range(int(spec.get("samples") or 0))]
    return summarize(samples)


def _page_count(root: Path) -> int:
    return sum(1 for _ in (root / "wiki").rglob("*.md"))


def backup_restore(deployment: Path, timeout: int) -> dict:
    source_pages = _page_count(deployment)
    with tempfile.TemporaryDirectory(prefix="okengine-qualification-backup-") as temporary:
        root = Path(temporary)
        backup_dir = root / "backups"
        create = run_command([
            str(ENGINE / "bin/framework"), "backup", "create", str(deployment),
            "--dest", str(backup_dir)], deployment, timeout)
        archives = list(backup_dir.glob("*.tar.gz"))  # glob-ok: backup output is one flat directory
        if len(archives) != 1:
            raise RuntimeError(f"backup produced {len(archives)} archives; exactly one required")
        verify = run_command([
            str(ENGINE / "bin/framework"), "backup", "verify", str(archives[0])],
            deployment, timeout)
        restored = root / "restored"
        restore = run_command([
            str(ENGINE / "bin/framework"), "backup", "restore", str(archives[0]),
            str(restored), "--no-validate"], deployment, timeout)
        restored_pages = _page_count(restored)
        if restored_pages != source_pages:
            raise RuntimeError(
                f"restored corpus has {restored_pages} pages; source backup had {source_pages}")
        return {"create_seconds": create["seconds"], "verify_seconds": verify["seconds"],
                "restore_seconds": restore["seconds"], "source_pages": source_pages,
                "restored_pages": restored_pages,
                "archive_bytes": archives[0].stat().st_size}


def execute(plan: dict, deployment: Path) -> dict:
    manifest_path = deployment / ".okengine/qualification-corpus.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required_pages = int(plan.get("required_pages") or 100_000)
    if manifest.get("pages") != required_pages:
        raise RuntimeError(f"qualification corpus has {manifest.get('pages')} pages; "
                           f"exactly {required_pages} required")
    timeout = int(plan.get("command_timeout_seconds") or 3600)
    disk_before = shutil.disk_usage(deployment)
    phases = {}
    for phase in plan.get("commands", []):
        command_samples = []
        memory_samples = []
        recovery_samples = []
        for _ in range(int(phase.get("samples") or 1)):
            result = run_command(phase["command"], deployment, timeout)
            command_samples.append(result["seconds"])
            if phase.get("measure") == "memory":
                memory_samples.append(memory_bytes(result["stdout"]))
            if phase.get("ready_http"):
                recovery_samples.append(wait_http(phase["ready_http"], timeout))
        phases[phase["name"]] = summarize(command_samples)
        if recovery_samples:
            phases[phase["name"]]["readiness"] = summarize(recovery_samples)
        if memory_samples:
            phases[phase["name"]].update(
                raw_memory_bytes=memory_samples, max_memory_bytes=max(memory_samples))
    overlap = plan.get("overlap") or []
    if overlap:
        with ThreadPoolExecutor(max_workers=len(overlap)) as pool:
            futures = {item["name"]: pool.submit(
                run_overlap, item, deployment, timeout) for item in overlap}
            phases["overlap"] = {name: future.result() for name, future in futures.items()}
    if plan.get("backup_restore"):
        phases["backup_restore"] = backup_restore(deployment, timeout)
    endpoints = {spec["name"]: sample_http(spec) for spec in plan.get("http", [])}
    endpoints.update({spec["name"]: sample_mcp(spec) for spec in plan.get("mcp", [])})
    if not endpoints:
        raise RuntimeError("qualification plan has no required service endpoints")
    disk = shutil.disk_usage(deployment)
    report = {
        "schema_version": 1, "corpus": manifest, "phases": phases,
        "endpoints": endpoints,
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "cpu_count": os.cpu_count(), "disk_total": disk.total,
                        "disk_used": disk.used, "disk_free": disk.free,
                        "disk_growth_bytes": disk.used - disk_before.used},
    }
    enforce_budgets(report, plan.get("budgets") or {})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--artifact", required=True)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        report = execute(plan, Path(args.deployment).resolve())
        target = Path(args.artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"QUALIFICATION FAILED: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
