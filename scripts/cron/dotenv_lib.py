#!/usr/bin/env python3
"""dotenv_lib.py — read a pack `.env` the way `docker compose` env_file does (okengine#991).

The containers get the pack `.env` through compose's env_file, so a host-side reader that parses
it differently decides a security switch on a value the containers never see. A copied
`split("=")` + quote-strip parser read `OKENGINE_EDITING=0 # read-only` as "0 # read-only" (editing
ON) while the gateway and reader saw "0" (editing OFF), and missed `export OKENGINE_EDITING=0`
entirely. This is the ONE parser: ensure-runtime, framework validate and the in-gateway deployment
checks all call it.

The grammar mirrors compose-go's dotenv parser (docker compose v2.39), pinned by a parity test that
diffs this module against `docker compose config` when docker is available:
  - blank lines and lines whose first non-blank character is `#` are skipped;
  - a leading `export ` is dropped; `KEY = value`, `KEY: value` and `KEY=value` all assign;
  - an unquoted value runs to end of line, is cut at the first ` #`, and is right-trimmed
    (so CRLF line endings and trailing blanks vanish; `KEY=0#x` keeps `0#x`);
  - a quoted value runs to its matching quote, may span lines, and a comment after the closing
    quote (`"0"  # off`) is ignored; a backslash escapes the quote; double quotes also apply Go's
    single-character escapes (`\\n`, `\\t`, `\\a`, `\\\\` ...) and keep any other `\\X` as written;
  - unquoted and double-quoted values interpolate `$VAR`, `${VAR}`, `${VAR:-default}` and
    `${VAR-default}` from `lookup` FIRST, then from earlier keys in the file (a later line is never
    seen); `\\$` is a literal `$` only inside double quotes; single quotes are literal;
  - a bare `KEY` line takes its value from `lookup`, or is omitted;
  - the last assignment of a key wins.
What compose passes as `lookup` (okengine#993 R1, read back from `docker compose config`): the
project `.env` is parsed with the invoking process environment; an env_file is parsed with the
process environment, then the parsed project `.env`. The pack `.env` is both, so a forward reference
DOES resolve in the container (through the project parse) and a process value beats an earlier
line. compose_env / read_compose_env model that; parse_dotenv is the single-pass primitive.
Shell callers use the CLI rather than a grep/cut or regex of their own (okengine#994 [17], A):
  python3 dotenv_lib.py get <pack>/.env KEY
prints the value compose gives the containers for KEY (read_compose_env, with the invoking
environment as compose's process environment) and exits 0; it exits 1 when KEY is unset or empty,
and 2 on a usage error.
Where compose would REJECT the file (an invalid key character, an unterminated quote), this reader
skips the bad statement and keeps going: compose will not start the stack at all in that case, so
no container value exists to disagree with.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping

_KEY_CHARS = set("_.-[]")
_SPACE = " \t\v\f\r\x85\xa0"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?-)([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
#: The single-character escapes Go's strconv.Unquote accepts, which compose-go applies inside
#: double quotes; any other `\X` (`\q`, `\ `, `\!`) keeps its backslash.
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
            "\\": "\\"}


def _interpolate(value: str, values: Mapping, lookup: Mapping) -> str:
    def sub(match: re.Match) -> str:
        name = match.group(1) or match.group(4)
        found = lookup.get(name, values.get(name))
        op = match.group(2)
        if op == ":-" and not found:
            return match.group(3)
        if op == "-" and found is None:
            return match.group(3)
        return found or ""
    return _VAR.sub(sub, value)


def _double_quoted(value: str, values: Mapping, lookup: Mapping) -> str:
    """Escapes, then interpolation, with `\\$` a literal `$` (it is never interpolated)."""
    parts = re.split(r"(\\.)", value, flags=re.S)
    out = []
    for n, part in enumerate(parts):
        if n % 2:
            out.append("$" if part == "\\$" else _ESCAPES.get(part[1], part))
        else:
            out.append(_interpolate(part, values, lookup))
    return "".join(out)


def _key(src: str) -> tuple[str | None, str, bool]:
    """(key, rest after the separator, inherited) for the statement at the start of `src`; key is
    None when the statement is invalid (rest then starts at the next line)."""
    if src.startswith("export") and len(src) > 6 and src[6] in _SPACE:
        src = src[6:].lstrip(_SPACE)
    for i, char in enumerate(src):
        if char in _SPACE:
            continue
        if char in "=:\n":
            key = src[:i].rstrip()
            return (key or None), src[i + 1:].lstrip(_SPACE), char == "\n"
        if not (char.isalnum() or char in _KEY_CHARS):
            break
    else:
        key = src.rstrip()
        return (key or None), "", True
    _bad, _sep, rest = src.partition("\n")
    return None, rest, False


def _value(src: str, values: Mapping, lookup: Mapping) -> tuple[str | None, str]:
    """(value, rest) for the value at the start of `src`; value is None for an unterminated quote."""
    quote = src[:1]
    if quote not in ("'", '"'):
        line, _sep, rest = src.partition("\n")
        line = line.split(" #", 1)[0].rstrip()
        return _interpolate(line, values, lookup), rest
    chars: list[str] = []
    escaped = False
    for i in range(1, len(src)):
        char = src[i]
        if char == quote and not escaped:
            value = "".join(chars)
            if quote == '"':
                value = _double_quoted(value, values, lookup)
            return value, src[i + 1:]
        if escaped:
            escaped = False
            if char != quote:
                chars.append("\\")
        elif char == "\\":
            escaped = True
            continue
        chars.append(char)
    return None, ""


def parse_dotenv(text: str, lookup: Mapping | None = None) -> dict[str, str]:
    """Every assignment in dotenv `text`, as compose env_file reads it. `lookup` supplies values
    for bare `KEY` lines and for interpolation (compose uses the invoking shell's environment)."""
    lookup = lookup if lookup is not None else {}
    values: dict[str, str] = {}
    rest = text.lstrip("﻿")
    while True:
        rest = rest.lstrip(_SPACE + "\n")
        if not rest:
            return values
        if rest.startswith("#"):
            rest = rest.partition("\n")[2]
            continue
        key, rest, inherited = _key(rest)
        if key is None:
            continue
        if inherited:
            if key in lookup:
                values[key] = str(lookup[key])
            continue
        value, rest = _value(rest, values, lookup)
        if value is not None:
            values[key] = value


def _read_text(path: Path | str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_dotenv(path: Path | str, lookup: Mapping | None = None) -> dict[str, str]:
    """parse_dotenv of the file at `path`; {} when it does not exist or cannot be read."""
    text = _read_text(path)
    return {} if text is None else parse_dotenv(text, lookup)


def compose_env(text: str, environ: Mapping, project_text: str | None = None) -> dict[str, str]:
    """The values compose gives a container from env_file `text`, when compose runs in the process
    environment `environ` with project `.env` text `project_text` (None: no project .env).

    compose-go parses the project .env with `environ` as its lookup, then the env_file with
    `environ` before the parsed project values: `${X}` resolves from the process, then the project
    .env (whole file, so a later line counts), then the env_file's own earlier keys."""
    project = parse_dotenv(project_text, environ) if project_text is not None else {}
    return parse_dotenv(text, {**project, **environ})


def read_compose_env(path: Path | str, environ: Mapping) -> dict[str, str]:
    """compose_env for the pack shape, where `path` (the pack `.env`) is both the project .env and
    the service's env_file; {} when it does not exist or cannot be read."""
    text = _read_text(path)
    return {} if text is None else compose_env(text, environ, project_text=text)


def compose_default(key: str, environ: Mapping, project_path: Path, default: str) -> str:
    """`${KEY:-default}` as compose interpolates it in a service's `environment:`: the invoking
    environment first (an exported EMPTY value is unset for `:-`, so it yields `default` and the
    project .env is not consulted), then the project .env at `project_path`, then `default`.
    The gateway's TZ is `TZ=${TZ:-UTC}`; ensure-runtime binds the same value into every stdio
    writer's env and host-side checks judge against it (okengine#1042 [36])."""
    if key in environ:
        return str(environ[key]) or default
    return read_dotenv(project_path, environ).get(key) or default


_USAGE = "usage: dotenv_lib.py get <path-to-.env> <KEY>"


def main(argv: list[str], environ: Mapping | None = None) -> int:
    """`get PATH KEY`: print KEY's compose value (no newline) and return 0, or 1 when it is unset or
    empty. An empty value is a miss because every shell caller treats one as "not pinned"."""
    if len(argv) != 3 or argv[0] != "get":
        print(_USAGE, file=sys.stderr)
        return 2
    _command, path, key = argv
    value = read_compose_env(path, os.environ if environ is None else environ).get(key)
    if not value:
        return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
