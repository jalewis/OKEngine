#!/usr/bin/env python3
"""Declarative, bounded source-connector runtime.

Packs describe a source in YAML; this engine-owned dispatcher validates the
contract, performs bounded HTTP/fixture acquisition, normalizes records, and
checkpoints cursor/conditional-request state. Connector manifests never contain
executable code or secret values.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collection_ledger  # noqa: E402
for _policy_tools in (Path(__file__).resolve().parents[2] / "tools", Path("/opt/hermes/tools")):
    if _policy_tools.is_dir():
        sys.path.insert(0, str(_policy_tools))
try:
    import policy_plane  # noqa: E402
except ImportError:  # pragma: no cover - old staged runtimes during rolling upgrade
    policy_plane = None

try:
    import yaml
except Exception:  # pragma: no cover - deploy/runtime dependency
    yaml = None

VERSION = 1
MODES = {"bundle", "query", "enrichment", "stream", "poll"}
AUTH_TYPES = {"none", "bearer", "api-key", "basic"}
PERMISSION_LEVELS = {"public", "authenticated", "licensed", "internal"}
SENSITIVITY = {"clear", "internal", "restricted"}
PAGINATION_TYPES = {"none", "page", "cursor"}
FORMATS = {"json", "jsonl"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TEMPLATE_RE = re.compile(r"\$\{(input|secret|runtime)\.([A-Za-z0-9_.-]+)\}")
MAX_TIMEOUT = 120
MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 100

TOP_KEYS = {
    "connector_version", "id", "name", "mode", "description", "trust", "permissions",
    "auth", "inputs", "request", "response", "pagination", "checkpoint",
    "conditional_requests", "rate_limit", "archive", "license", "health",
    "enrich",   # identity-authority application block (okengine#314) — consumed by authority_enrich.py
}
BLOCK_KEYS = {
    "trust": {"permission", "data_sensitivity", "source_authority"},
    "permissions": {"network", "allowed_hosts", "allow_private_network", "write_raw"},
    "auth": {"type", "secret_refs"},
    "inputs": {"required"},
    "request": {"url", "method", "headers", "query", "timeout_seconds", "max_bytes"},
    "response": {"format", "records_path", "stable_id_path", "revision_path", "deleted_path"},
    "pagination": {"type", "max_pages", "start", "request_param", "response_path"},
    "checkpoint": {"path"},
    "conditional_requests": {"enabled"},
    "rate_limit": {"max_requests", "per_seconds"},
    "archive": {"enabled", "raw_responses", "path", "retention_days"},
    "license": {"name", "url", "redistribution", "max_retention_days"},
    "health": {"path"},
}
BLOCK_REQUIRED = {
    "trust": {"permission", "data_sensitivity", "source_authority"},
    "permissions": {"network", "allowed_hosts", "allow_private_network", "write_raw"},
    "auth": {"type", "secret_refs"},
    "request": {"url", "method"},
    "response": {"format", "records_path", "stable_id_path"},
    "pagination": {"type", "max_pages"},
    "checkpoint": {"path"},
    "conditional_requests": {"enabled"},
    "rate_limit": {"max_requests", "per_seconds"},
    "archive": {"enabled", "raw_responses", "path", "retention_days"},
    "license": {"name", "redistribution", "max_retention_days"},
    "health": {"path"},
}


class ConnectorError(Exception):
    """A connector cannot be validated or executed safely."""


class NotModified(Exception):
    """The upstream returned HTTP 304."""


@dataclass(frozen=True)
class ResponsePage:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_yaml(path: Path) -> dict:
    if yaml is None:
        raise ConnectorError("PyYAML is required to read connector manifests")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConnectorError(f"{path}: expected a YAML mapping")
    return value


def _mapping(value: Any, name: str, errors: list[str]) -> dict:
    if not isinstance(value, dict):
        errors.append(f"{name} must be a mapping")
        return {}
    for key in sorted(set(value) - BLOCK_KEYS.get(name, set(value))):
        errors.append(f"unknown key under {name}: {key}")
    for key in sorted(BLOCK_REQUIRED.get(name, set()) - set(value)):
        errors.append(f"missing required key: {name}.{key}")
    return value


def _relative_path(value: Any, name: str, errors: list[str], *, required: bool = True) -> None:
    if not value and not required:
        return
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{name} must be a non-empty relative path")
        return
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        errors.append(f"{name} must stay within its configured runtime root")


def _url_host(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    parsed = urllib.parse.urlparse(url)
    return (parsed.hostname or "").casefold()


def _inline_secret(value: str) -> bool:
    return bool(value and "${secret." not in value)


def validate_manifest(manifest: dict) -> list[str]:
    """Return deploy-blocking contract errors. No warning-only ambiguity exists."""
    errors: list[str] = []
    enrich = manifest.get("enrich")
    if enrich is not None:
        # identity-authority application contract (okengine#314): the block is only meaningful for
        # enrichment mode, and every field authority_enrich.py depends on must be present at deploy.
        if manifest.get("mode") != "enrichment":
            errors.append("enrich: only valid for mode: enrichment")
        if not isinstance(enrich, dict):
            errors.append("enrich: must be a mapping")
        else:
            for key in sorted(set(enrich) - {"authority", "id_path", "match", "targets"}):
                errors.append(f"enrich: unknown key: {key}")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", str(enrich.get("authority") or "")):
                errors.append("enrich.authority: required, lowercase [a-z0-9_], <=31 chars")
            if not str(enrich.get("id_path") or "").strip():
                errors.append("enrich.id_path: required")
            match = enrich.get("match")
            if not isinstance(match, dict):
                errors.append("enrich.match: required mapping")
            else:
                for req in ("query_input", "page_field"):
                    if not str(match.get(req) or "").strip():
                        errors.append(f"enrich.match.{req}: required")
                cands = match.get("candidate_paths")
                if not (isinstance(cands, list) and cands and all(str(c).strip() for c in cands)):
                    errors.append("enrich.match.candidate_paths: non-empty list required")
                qi = str(match.get("query_input") or "")
                required_inputs = (manifest.get("inputs") or {}).get("required") or []
                if qi and qi not in required_inputs:
                    errors.append(f"enrich.match.query_input '{qi}' is not a required manifest input")
            targets = enrich.get("targets")
            if not isinstance(targets, dict) or not (
                    isinstance(targets.get("types"), list) and targets["types"]):
                errors.append("enrich.targets.types: non-empty list required")
    for key in sorted(set(manifest) - TOP_KEYS):
        errors.append(f"unknown top-level key: {key}")
    for key in ("connector_version", "id", "mode", "trust", "permissions", "auth", "request",
                "response", "pagination", "checkpoint", "conditional_requests", "rate_limit",
                "archive", "license", "health"):
        if key not in manifest:
            errors.append(f"missing required key: {key}")

    if manifest.get("connector_version") != VERSION:
        errors.append(f"connector_version must be {VERSION}")
    connector_id = manifest.get("id")
    if not isinstance(connector_id, str) or not ID_RE.fullmatch(connector_id):
        errors.append("id must be 3-128 lowercase dotted/hyphenated characters")
    if manifest.get("mode") not in MODES:
        errors.append(f"mode must be one of {sorted(MODES)}")

    trust = _mapping(manifest.get("trust"), "trust", errors)
    if trust.get("permission") not in PERMISSION_LEVELS:
        errors.append(f"trust.permission must be one of {sorted(PERMISSION_LEVELS)}")
    if trust.get("data_sensitivity") not in SENSITIVITY:
        errors.append(f"trust.data_sensitivity must be one of {sorted(SENSITIVITY)}")
    if not isinstance(trust.get("source_authority"), str) or not trust.get("source_authority", "").strip():
        errors.append("trust.source_authority is required")

    permissions = _mapping(manifest.get("permissions"), "permissions", errors)
    if permissions.get("network") is not True:
        errors.append("permissions.network must explicitly be true")
    allowed_hosts = permissions.get("allowed_hosts")
    if not isinstance(allowed_hosts, list) or not allowed_hosts or not all(
            isinstance(x, str) and x == x.casefold() and x.strip() for x in allowed_hosts):
        errors.append("permissions.allowed_hosts must be a non-empty lowercase host list")
        allowed_hosts = []
    if not isinstance(permissions.get("write_raw"), bool):
        errors.append("permissions.write_raw must be boolean")
    if not isinstance(permissions.get("allow_private_network"), bool):
        errors.append("permissions.allow_private_network must be boolean")
    elif permissions.get("allow_private_network") and trust.get("permission") != "internal":
        errors.append("allow_private_network requires trust.permission: internal")

    auth = _mapping(manifest.get("auth"), "auth", errors)
    auth_type = auth.get("type")
    if auth_type not in AUTH_TYPES:
        errors.append(f"auth.type must be one of {sorted(AUTH_TYPES)}")
    refs = auth.get("secret_refs", {})
    if not isinstance(refs, dict) or not all(isinstance(k, str) and ENV_RE.fullmatch(str(v or ""))
                                             for k, v in refs.items()):
        errors.append("auth.secret_refs values must be environment-variable names")
        refs = {}
    if auth_type != "none" and not refs:
        errors.append("authenticated connectors require auth.secret_refs")
    if auth_type == "none" and refs:
        errors.append("auth.type none cannot declare secret_refs")

    inputs = manifest.get("inputs", {})
    if not isinstance(inputs, dict):
        errors.append("inputs must be a mapping")
    elif not isinstance(inputs.get("required", []), list) or not all(
            isinstance(x, str) and x.strip() for x in inputs.get("required", [])):
        errors.append("inputs.required must be a list of names")

    request = _mapping(manifest.get("request"), "request", errors)
    if request.get("method", "GET") != "GET":
        errors.append("request.method: only bounded declarative GET is supported")
    url = request.get("url")
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        errors.append("request.url must be an http(s) URL")
    elif parsed.username is not None or parsed.password is not None:
        errors.append("request.url must not contain credentials")
    elif parsed.hostname.casefold() not in allowed_hosts:
        errors.append("request.url host must be declared in permissions.allowed_hosts")
    timeout = request.get("timeout_seconds", 20)
    max_bytes = request.get("max_bytes", 5 * 1024 * 1024)
    if not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
        errors.append(f"request.timeout_seconds must be 1..{MAX_TIMEOUT}")
    if not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_BYTES:
        errors.append(f"request.max_bytes must be 1..{MAX_BYTES}")
    headers = request.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in headers.items()):
        errors.append("request.headers must map header names to strings")
        headers = {}
    for key, value in headers.items():
        if key.casefold() in {"authorization", "proxy-authorization", "x-api-key", "api-key"} \
                and _inline_secret(value):
            errors.append(f"request.headers.{key} must reference a secret, not embed one")
    query = request.get("query", {})
    if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                              for k, v in query.items()):
        errors.append("request.query must map parameter names to strings")
        query = {}
    template_values = ([('url', url)] if isinstance(url, str) else [])
    template_values.extend(("header", value) for value in headers.values())
    template_values.extend(("query", value) for value in query.values())
    required_inputs = set(inputs.get("required", [])) if isinstance(inputs, dict) else set()
    for location, value in template_values:
        matches = list(TEMPLATE_RE.finditer(value))
        remainder = TEMPLATE_RE.sub("", value)
        if "${" in remainder:
            errors.append(f"unsupported or malformed template in request value: {value!r}")
        for match in matches:
            scope, name = match.groups()
            if scope == "secret" and location == "url":
                errors.append("request.url must not contain secret templates")
            if scope == "secret" and location == "query":
                errors.append("request.query values must not contain secret templates")
            if scope == "secret" and name not in refs:
                errors.append(f"request references undeclared secret: {name}")
            elif scope == "input" and name not in required_inputs:
                errors.append(f"request references input not declared in inputs.required: {name}")
            elif scope == "runtime" and name not in {"cursor", "page"}:
                errors.append(f"request references unsupported runtime value: {name}")

    response = _mapping(manifest.get("response"), "response", errors)
    if response.get("format") not in FORMATS:
        errors.append(f"response.format must be one of {sorted(FORMATS)}")
    if not isinstance(response.get("records_path", ""), str):
        errors.append("response.records_path must be a dotted path (empty means response root)")
    for key in ("stable_id_path", "revision_path", "deleted_path"):
        if key == "stable_id_path" and not response.get(key):
            errors.append("response.stable_id_path is required")
        elif response.get(key) is not None and not isinstance(response.get(key), str):
            errors.append(f"response.{key} must be a dotted path")

    pagination = _mapping(manifest.get("pagination"), "pagination", errors)
    pagination_type = pagination.get("type")
    if pagination_type not in PAGINATION_TYPES:
        errors.append(f"pagination.type must be one of {sorted(PAGINATION_TYPES)}")
    max_pages = pagination.get("max_pages", 1)
    if not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_PAGES:
        errors.append(f"pagination.max_pages must be 1..{MAX_PAGES}")
    if "start" in pagination and (not isinstance(pagination["start"], int)
                                   or pagination["start"] < 0):
        errors.append("pagination.start must be a non-negative integer")
    if pagination_type == "page" and not pagination.get("request_param"):
        errors.append("page pagination requires pagination.request_param")
    if pagination_type == "cursor" and not (pagination.get("request_param")
                                              and pagination.get("response_path")):
        errors.append("cursor pagination requires request_param and response_path")

    checkpoint = _mapping(manifest.get("checkpoint"), "checkpoint", errors)
    _relative_path(checkpoint.get("path"), "checkpoint.path", errors)
    conditional = _mapping(manifest.get("conditional_requests"), "conditional_requests", errors)
    if not isinstance(conditional.get("enabled"), bool):
        errors.append("conditional_requests.enabled must be boolean")

    rate = _mapping(manifest.get("rate_limit"), "rate_limit", errors)
    if not isinstance(rate.get("max_requests"), int) or rate.get("max_requests", 0) < 1:
        errors.append("rate_limit.max_requests must be a positive integer")
    if not isinstance(rate.get("per_seconds"), (int, float)) or rate.get("per_seconds", -1) < 0:
        errors.append("rate_limit.per_seconds must be non-negative")

    archive = _mapping(manifest.get("archive"), "archive", errors)
    if not isinstance(archive.get("enabled"), bool) or not isinstance(archive.get("raw_responses"), bool):
        errors.append("archive.enabled and archive.raw_responses must be boolean")
    _relative_path(archive.get("path"), "archive.path", errors, required=bool(archive.get("enabled")))
    if not isinstance(archive.get("retention_days"), int) or archive.get("retention_days", -1) < 0:
        errors.append("archive.retention_days must be a non-negative integer")
    if archive.get("enabled") and permissions.get("write_raw") is not True:
        errors.append("archive.enabled requires permissions.write_raw: true")

    license_block = _mapping(manifest.get("license"), "license", errors)
    if not isinstance(license_block.get("name"), str) or not license_block.get("name", "").strip():
        errors.append("license.name is required")
    if "url" in license_block and not isinstance(license_block["url"], str):
        errors.append("license.url must be a string")
    if license_block.get("redistribution") not in {"allowed", "restricted", "prohibited"}:
        errors.append("license.redistribution must be allowed, restricted, or prohibited")
    max_retention = license_block.get("max_retention_days")
    if not isinstance(max_retention, int) or max_retention < 0:
        errors.append("license.max_retention_days must be a non-negative integer")
    elif archive.get("retention_days", 0) > max_retention:
        errors.append("archive.retention_days exceeds license.max_retention_days")
    if archive.get("enabled") and license_block.get("redistribution") == "prohibited" \
            and trust.get("data_sensitivity") == "clear":
        errors.append("prohibited redistribution cannot use clear data_sensitivity when archived")

    health = _mapping(manifest.get("health"), "health", errors)
    _relative_path(health.get("path"), "health.path", errors)
    return errors


def _lookup(value: Any, path: str, default: Any = None) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def _render(value: str, inputs: dict[str, str], secrets: dict[str, str], runtime: dict[str, Any],
            *, url_component: bool = False) -> str:
    scopes = {"input": inputs, "secret": secrets, "runtime": runtime}

    def replace(match: re.Match) -> str:
        scope, name = match.groups()
        resolved = _lookup(scopes[scope], name)
        if resolved is None:
            raise ConnectorError(f"unresolved template: {match.group(0)}")
        rendered = str(resolved)
        return urllib.parse.quote(rendered, safe="") if url_component else rendered

    return TEMPLATE_RE.sub(replace, value)


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"invalid checkpoint {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConnectorError(f"invalid checkpoint {path}: expected an object")
    return value


def _write_json_once(path: Path, value: Any) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    try:
        with path.open("xb") as handle:
            handle.write(payload)
        return True
    except FileExistsError:
        return False


def _write_bytes_once(path: Path, payload: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
        return True
    except FileExistsError:
        return False


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _runtime_path(root: Path, relative: str) -> Path:
    base = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ConnectorError(f"runtime path escapes configured root: {relative}") from exc
    return candidate


def _validate_network_url(url: str, allowed_hosts: list[str], allow_private: bool = False) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host or host not in allowed_hosts:
        raise ConnectorError("request URL escapes declared network permissions")
    if parsed.username is not None or parsed.password is not None:
        raise ConnectorError("request URL contains credentials")
    if allow_private:
        return
    try:
        addresses = socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectorError(f"cannot resolve connector host: {exc}") from exc
    for info in addresses:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved):
            raise ConnectorError(f"refusing non-public connector address: {address}")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: list[str], allow_private: bool):
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_network_url(newurl, self.allowed_hosts, self.allow_private)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_http(url: str, headers: dict[str, str], manifest: dict) -> ResponsePage:
    allowed = manifest["permissions"]["allowed_hosts"]
    allow_private = manifest["permissions"]["allow_private_network"]
    _validate_network_url(url, allowed, allow_private)
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_SafeRedirect(allowed, allow_private))
    timeout = manifest["request"].get("timeout_seconds", 20)
    limit = manifest["request"].get("max_bytes", 5 * 1024 * 1024)
    try:
        with opener.open(req, timeout=timeout) as response:
            final_url = response.geturl()
            _validate_network_url(final_url, allowed, allow_private)
            length = str(response.headers.get("Content-Length") or "")
            if length.isdigit() and int(length) > limit:
                raise ConnectorError(f"response Content-Length exceeds {limit}")
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ConnectorError(f"response exceeds {limit} bytes")
            return ResponsePage(int(getattr(response, "status", 200)),
                                {str(k).casefold(): str(v) for k, v in response.headers.items()},
                                body, final_url)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            raise NotModified from None
        raise ConnectorError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectorError(f"network failure: {exc}") from exc


def _load_fixture(path: Path) -> list[ResponsePage]:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorError(f"invalid fixture {path}: {exc}") from exc
    if not isinstance(fixture, dict) or fixture.get("fixture_version") != 1 \
            or not isinstance(fixture.get("pages"), list) or not fixture["pages"]:
        raise ConnectorError("fixture requires fixture_version: 1 and a non-empty pages list")
    pages = []
    for index, page in enumerate(fixture["pages"]):
        if not isinstance(page, dict) or not isinstance(page.get("body"), (dict, list, str)):
            raise ConnectorError(f"fixture pages[{index}].body must be JSON-compatible")
        body = page["body"] if isinstance(page["body"], str) else json.dumps(page["body"])
        pages.append(ResponsePage(int(page.get("status", 200)),
                                  {str(k).casefold(): str(v)
                                   for k, v in (page.get("headers") or {}).items()},
                                  body.encode(), str(page.get("final_url") or "fixture://page")))
    return pages


def _decode_records(page: ResponsePage, response: dict) -> tuple[Any, list[dict]]:
    try:
        if response["format"] == "json":
            decoded = json.loads(page.body)
        else:
            decoded = [json.loads(line) for line in page.body.splitlines() if line.strip()]
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConnectorError(f"response is not valid {response['format']}: {exc}") from exc
    records = _lookup(decoded, response.get("records_path", ""))
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ConnectorError("response.records_path did not resolve to record objects")
    return decoded, records


def _safe_component(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:80]
    return slug or hashlib.sha256(value.encode()).hexdigest()[:16]


def _normalize(manifest: dict, record: dict, observed_at: str) -> dict:
    response = manifest["response"]
    native_id = _lookup(record, response["stable_id_path"])
    if native_id is None or isinstance(native_id, (dict, list)) or not str(native_id).strip():
        raise ConnectorError("record has no scalar stable ID at response.stable_id_path")
    revision = _lookup(record, response.get("revision_path", "")) \
        if response.get("revision_path") else None
    if revision is None or isinstance(revision, (dict, list)) or not str(revision).strip():
        revision = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                                ensure_ascii=False).encode()).hexdigest()
    deleted_value = _lookup(record, response.get("deleted_path", ""), False) \
        if response.get("deleted_path") else False
    return {
        "connector_id": manifest["id"],
        "mode": manifest["mode"],
        "source_native_id": str(native_id),
        "source_revision": str(revision),
        "deleted": bool(deleted_value),
        "observed_at": observed_at,
        "source_authority": manifest["trust"]["source_authority"],
        "source_permission": manifest["trust"]["permission"],
        "data_sensitivity": manifest["trust"]["data_sensitivity"],
        "license": dict(manifest["license"]),
        "retention_days": manifest["archive"]["retention_days"],
        "payload": record,
    }


def _secrets(manifest: dict, env: dict[str, str]) -> dict[str, str]:
    values = {}
    for name, reference in manifest["auth"].get("secret_refs", {}).items():
        if not env.get(reference):
            raise ConnectorError(f"required secret environment reference is unset: {reference}")
        values[name] = env[reference]
    return values


def _request_parts(manifest: dict, inputs: dict[str, str], secrets: dict[str, str],
                   runtime: dict[str, Any], state: dict) -> tuple[str, dict[str, str]]:
    request = manifest["request"]
    url = _render(request["url"], inputs, secrets, runtime, url_component=True)
    query = {}
    for key, value in request.get("query", {}).items():
        query[str(key)] = _render(str(value), inputs, secrets, runtime)
    pagination = manifest["pagination"]
    if pagination["type"] == "page":
        query[pagination["request_param"]] = str(runtime["page"])
    elif pagination["type"] == "cursor" and runtime.get("cursor"):
        query[pagination["request_param"]] = str(runtime["cursor"])
    if query:
        parsed = urllib.parse.urlsplit(url)
        merged = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) + list(query.items())
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                      urllib.parse.urlencode(merged), parsed.fragment))
    headers = {}
    for key, value in request.get("headers", {}).items():
        rendered = _render(str(value), inputs, secrets, runtime)
        if "\r" in rendered or "\n" in rendered:
            raise ConnectorError(f"rendered header contains a newline: {key}")
        headers[str(key)] = rendered
    headers.setdefault("Accept", "application/json, application/x-ndjson;q=0.9")
    headers.setdefault("User-Agent", "okengine-source-connector/1.0")
    if manifest["conditional_requests"]["enabled"]:
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
    return url, headers


def _redact_plan(manifest: dict, inputs: dict[str, str], state: dict) -> dict:
    runtime = {"page": manifest["pagination"].get("start", 1),
               "cursor": state.get("cursor", "")}
    secret_names = {name: f"<secret:{reference}>"
                    for name, reference in manifest["auth"].get("secret_refs", {}).items()}
    url, headers = _request_parts(manifest, inputs, secret_names, runtime, state)
    return {"connector_id": manifest["id"], "mode": manifest["mode"], "dry_run": True,
            "request": {"method": "GET", "url": url, "headers": headers},
            "checkpoint": manifest["checkpoint"]["path"],
            "archive_enabled": manifest["archive"]["enabled"]}


def execute(manifest: dict, *, inputs: dict[str, str] | None = None,
            env: dict[str, str] | None = None, state_root: Path,
            archive_root: Path, health_root: Path, fixture: Path | None = None,
            dry_run: bool = False, observed_at: str | None = None,
            sleep=time.sleep, ledger_root: Path | None = None) -> dict:
    errors = validate_manifest(manifest)
    if errors:
        raise ConnectorError("manifest invalid:\n- " + "\n- ".join(errors))
    inputs = inputs or {}
    missing = sorted(set(manifest.get("inputs", {}).get("required", [])) - set(inputs))
    if missing:
        raise ConnectorError(f"missing required inputs: {', '.join(missing)}")
    checkpoint_path = _runtime_path(state_root, manifest["checkpoint"]["path"])
    _runtime_path(archive_root, manifest["archive"].get("path") or ".")
    health_path = _runtime_path(health_root, manifest["health"]["path"])
    state = _load_state(checkpoint_path)
    checkpoint_before = collection_ledger.checkpoint_digest(
        {key: value for key, value in state.items() if key != "updated_at"})
    if dry_run:
        return _redact_plan(manifest, inputs, state)

    observed_at = observed_at or now_utc()
    started_at = now_utc()
    started_tick = time.monotonic()
    telemetry_source = collection_ledger.source_id(
        manifest["id"], manifest["id"], manifest.get("name") or manifest["id"])
    if ledger_root is not None:
        collection_ledger.register_sources(Path(ledger_root), [{
            "connector_id": manifest["id"], "source_id": telemetry_source,
            "label": manifest.get("name") or manifest["id"],
            # Authority does not imply primary/independent origin. Connectors may
            # declare those once the contract gains claim-specific provenance.
            "source_kind": "unknown", "independent_origin": None,
        }], connector_id=manifest["id"])
    pagination = manifest["pagination"]
    max_pages = min(pagination.get("max_pages", 1), manifest["rate_limit"]["max_requests"])
    page_number = pagination.get("start", 1)
    cursor = state.get("cursor", "")
    envelopes: list[dict] = []
    requests = 0
    created = 0
    deletions = 0
    not_modified = False
    last_headers: dict[str, str] = {}

    try:
        secrets = _secrets(manifest, env or dict(os.environ))
        fixture_pages = _load_fixture(fixture) if fixture else None
        for index in range(max_pages):
            runtime = {"page": page_number, "cursor": cursor}
            url, headers = _request_parts(manifest, inputs, secrets, runtime, state)
            if index and not fixture_pages:
                delay = float(manifest["rate_limit"]["per_seconds"]) / max(
                    1, manifest["rate_limit"]["max_requests"])
                if delay:
                    sleep(delay)
            if fixture_pages is not None and index >= len(fixture_pages):
                break
            requests += 1
            try:
                if fixture_pages is not None:
                    page = fixture_pages[index]
                    if page.status == 304:
                        raise NotModified
                    if not 200 <= page.status < 300:
                        raise ConnectorError(f"fixture HTTP {page.status}")
                else:
                    page = _open_http(url, headers, manifest)
            except NotModified:
                not_modified = True
                break
            last_headers.update(page.headers)
            decoded, records = _decode_records(page, manifest["response"])
            if manifest["archive"]["enabled"] and manifest["archive"]["raw_responses"]:
                digest = hashlib.sha256(page.body).hexdigest()
                suffix = ".json" if manifest["response"]["format"] == "json" else ".jsonl"
                raw_path = _runtime_path(archive_root, str(
                    Path(manifest["archive"]["path"]) / "raw" / f"{digest}{suffix}"))
                _write_bytes_once(raw_path, page.body)
            for record in records:
                envelope = _normalize(manifest, record, observed_at)
                if policy_plane is not None:
                    policy_result = policy_plane.validate_importer_envelope(
                        envelope, actor=f"connector:{manifest['id']}")
                    if policy_result:
                        raise ConnectorError(policy_plane.finding_message(policy_result))
                envelopes.append(envelope)
                deletions += int(envelope["deleted"])
                if manifest["archive"]["enabled"]:
                    observation = hashlib.sha256(json.dumps(
                        {"source_revision": envelope["source_revision"],
                         "deleted": envelope["deleted"], "payload": envelope["payload"]},
                        sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False).encode()).hexdigest()[:12]
                    record_path = _runtime_path(archive_root, str(
                        Path(manifest["archive"]["path"]) / "records"
                        / _safe_component(envelope["source_native_id"])
                        / f"{_safe_component(envelope['source_revision'])}-{observation}.json"))
                    created += int(_write_json_once(record_path, envelope))
            next_cursor = _lookup(decoded, pagination.get("response_path", "")) \
                if pagination["type"] == "cursor" else None
            if pagination["type"] == "cursor":
                if next_cursor is None or str(next_cursor) == str(cursor):
                    break
                cursor = str(next_cursor)
            elif pagination["type"] == "page":
                if not records:
                    break
                page_number += 1
            else:
                break

        state.update({"connector_id": manifest["id"], "updated_at": observed_at, "cursor": cursor,
                      "etag": last_headers.get("etag", state.get("etag", "")),
                      "last_modified": last_headers.get(
                          "last-modified", state.get("last_modified", ""))})
        _atomic_json(checkpoint_path, state)
        result = {"connector_id": manifest["id"], "mode": manifest["mode"], "ok": True,
                  "observed_at": observed_at, "requests": requests, "records": len(envelopes),
                  "new_revisions": created, "deletions": deletions,
                  "not_modified": not_modified, "cursor": cursor, "items": envelopes}
    except Exception as exc:
        result = {"connector_id": manifest["id"], "mode": manifest["mode"], "ok": False,
                  "observed_at": observed_at, "requests": requests, "records": len(envelopes),
                  "error": str(exc)}
        _atomic_json(health_path, result)
        if ledger_root is not None:
            try:
                collection_ledger.append_attempt(Path(ledger_root), {
                    "connector_id": manifest["id"], "source_id": telemetry_source,
                    "started_at": started_at, "finished_at": now_utc(), "outcome": "failure",
                    "fetched": len(envelopes), "extracted": len(envelopes),
                    "accepted": created, "deduped": max(0, len(envelopes) - created),
                    "latency_ms": int((time.monotonic() - started_tick) * 1000),
                    "error_category": "connector-error",
                    "checkpoint_in": checkpoint_before,
                })
            except (OSError, ValueError):
                pass  # preserve the connector's original failure
        raise
    _atomic_json(health_path,
                 {key: value for key, value in result.items() if key != "items"})
    if ledger_root is not None:
        collection_ledger.append_attempt(Path(ledger_root), {
            "connector_id": manifest["id"], "source_id": telemetry_source,
            "started_at": started_at, "finished_at": now_utc(), "outcome": "success",
            "fetched": len(envelopes), "extracted": len(envelopes),
            "accepted": created, "deduped": max(0, len(envelopes) - created),
            "latency_ms": int((time.monotonic() - started_tick) * 1000),
            "checkpoint_in": checkpoint_before,
            "checkpoint_out": collection_ledger.checkpoint_digest(
                {key: value for key, value in state.items() if key != "updated_at"}),
        })
    return result


def _params(values: list[str]) -> dict[str, str]:
    out = {}
    for value in values:
        if "=" not in value or not value.split("=", 1)[0]:
            raise ConnectorError(f"invalid --param {value!r}; expected NAME=VALUE")
        key, item = value.split("=", 1)
        out[key] = item
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--fixture", type=Path, help="deterministic response fixture; disables network")
    parser.add_argument("--dry-run", action="store_true", help="validate and print a redacted request plan")
    parser.add_argument("--state-root", type=Path, default=Path(".okengine/connectors/state"))
    parser.add_argument("--archive-root", type=Path, default=Path("raw/connectors"))
    parser.add_argument("--health-root", type=Path, default=Path(".okengine/connectors/health"))
    parser.add_argument("--collection-ledger", type=Path,
                        default=(Path(os.environ["COLLECTION_LEDGER_DIR"])
                                 if os.environ.get("COLLECTION_LEDGER_DIR") else
                                 Path("/opt/data/collection") if Path("/opt/data").is_dir() else None),
                        help="append collection telemetry (deployed default: /opt/data/collection)")
    parser.add_argument("--observed-at", help="fixed timestamp for deterministic fixture runs")
    parser.add_argument("--summary-only", action="store_true",
                        help="omit acquired items from stdout (archives remain complete)")
    parser.add_argument("--wake-on-new", action="store_true",
                        help="set wakeAgent when this run creates revisions (for ingest crons)")
    args = parser.parse_args(argv)
    try:
        manifest = load_yaml(args.manifest)
        result = execute(manifest, inputs=_params(args.param), state_root=args.state_root,
                         archive_root=args.archive_root, health_root=args.health_root,
                         ledger_root=args.collection_ledger,
                         fixture=args.fixture, dry_run=args.dry_run,
                         observed_at=args.observed_at)
    except ConnectorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    output = dict(result)
    output["wakeAgent"] = bool(args.wake_on_new and not args.dry_run
                               and output.get("new_revisions", 0))
    if args.summary_only:
        output.pop("items", None)
    # cron-plus reads the final stdout line as its wake-gate object.
    print(json.dumps(output, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
