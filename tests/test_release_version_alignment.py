"""Release marker alignment across package, manifest, and security policy."""

import re
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def version_alignment_errors(pyproject_text: str, manifest_text: str, security_text: str) -> list[str]:
    errors: list[str] = []
    try:
        package_version = tomllib.loads(pyproject_text)["project"]["version"]
    except (ValueError, KeyError, TypeError):
        return ["pyproject.toml has no parseable project.version"]
    if not isinstance(package_version, str) or not re.fullmatch(r"0\.[0-9]+\.[0-9]+", package_version):
        return [f"unsupported package release version: {package_version!r}"]

    release = re.search(r"^engine_release:\s*(v[0-9]+\.[0-9]+\.[0-9]+)\b", manifest_text, re.M)
    if release is None or release.group(1) != f"v{package_version}":
        errors.append("engine-manifest.yaml engine_release differs from pyproject.toml")

    minor = ".".join(package_version.split(".")[:2])
    supported = re.findall(r"\|\s*`([0-9]+\.[0-9]+)\.x`\s*/\s*`main`\s*\|\s*✅\s*\|", security_text)
    if supported != [minor]:
        errors.append(
            f"SECURITY.md supported-series row must be exactly {minor}.x / main; got {supported!r}"
        )
    return errors


def test_release_markers_match_the_security_supported_series():
    errors = version_alignment_errors(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        (ROOT / "engine-manifest.yaml").read_text(encoding="utf-8"),
        (ROOT / "SECURITY.md").read_text(encoding="utf-8"),
    )

    assert not errors, f"release marker drift: {errors}"


@pytest.mark.parametrize(
    ("which", "replacement", "expected"),
    [
        ("manifest", "engine_release: v0.13.8\n", "engine-manifest.yaml"),
        ("manifest", "hermes_pin: v2026.7.7.2\n", "engine-manifest.yaml"),
        ("security", "| `0.12.x` / `main` | ✅ |", "SECURITY.md"),
        ("security", "no supported row", "SECURITY.md"),
        (
            "security",
            "| `0.13.x` / `main` | ✅ |\n| `0.12.x` / `main` | ✅ |",
            "SECURITY.md",
        ),
        ("pyproject", "version = 0.13.9", "pyproject.toml"),
        ("pyproject", "name = \"okengine\"", "pyproject.toml"),
        ("pyproject", "version = 13", "unsupported package release"),
        ("pyproject", "version = \"1.13.9\"", "unsupported package release"),
    ],
)
def test_negative_fixture_rejects_a_drifted_release_surface(which, replacement, expected):
    pyproject = '[project]\nversion = "0.13.9"\n'
    manifest = "engine_release: v0.13.9\n"
    security = "| `0.13.x` / `main` | ✅ |"
    if which == "pyproject":
        pyproject = f"[project]\n{replacement}\n"
    elif which == "manifest":
        manifest = replacement
    else:
        security = replacement

    errors = version_alignment_errors(pyproject, manifest, security)

    assert any(expected in error for error in errors), (
        f"{which} drift should fail with {expected} named, got {errors}"
    )
