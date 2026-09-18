"""Move active pack DeepSeek selections to the canonical V4.1 Flash model ID."""

import re
from pathlib import Path

ID = "okengine-deepseek-v4-1-flash"
FROM = "v0.13.8"
TO = "v0.14.0"
DESCRIPTION = "Replace active legacy DeepSeek model selections with deepseek-flash"

_ACTIVE_CONFIGS = (
    Path(".okengine/model-profiles.yaml"),
    Path(".okengine/cron-models.json"),
    Path(".okengine/extension-models.json"),
    Path("crons/domain-crons.json"),
    Path(".hermes-data/config.yaml"),
    Path(".env"),
    Path(".hermes/config.yaml"),
    Path(".hermes/config.yml"),
    Path("config.yaml"),
    Path("config.yml"),
)
_MODEL_REPLACEMENTS = (
    ("openrouter/deepseek/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4.1-flash"),
    ("openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4.1-flash"),
    ("openrouter/deepseek/deepseek-chat", "openrouter/deepseek/deepseek-v4.1-flash"),
    ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash"),
    ("deepseek/deepseek-v4-flash-vision-exp", "deepseek/deepseek-v4.1-flash"),
    ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4.1-flash"),
    ("deepseek/deepseek-reasoner", "deepseek/deepseek-v4.1-flash"),
    ("deepseek/deepseek-chat", "deepseek/deepseek-v4.1-flash"),
    ("deepseek-v4-pro", "deepseek-flash"),
    ("deepseek-v4-flash-vision-exp", "deepseek-flash"),
    ("deepseek-v4-flash", "deepseek-flash"),
    ("deepseek-reasoner", "deepseek-flash"),
    ("deepseek-chat", "deepseek-flash"),
)
_TARGET_ID = "deepseek-flash"


def _active_config_paths(pack: Path) -> list[Path]:
    relative = set(_ACTIVE_CONFIGS)
    for root in (Path("extensions"), Path(".okengine/extensions")):
        base = pack / root
        if base.is_dir():
            relative.update(path.relative_to(pack) for path in base.rglob("extension.yaml"))
            relative.update(path.relative_to(pack) for path in base.rglob("*.cron.json"))
    return sorted(relative)


def _replace_active_values(path: Path, text: str) -> tuple[str, int]:
    """Replace exact configured scalar values without touching keys, references, or comments."""
    replacements = dict(_MODEL_REPLACEMENTS)
    count = 0
    lines = []
    is_json = path.suffix == ".json"
    is_env = path.name == ".env"
    for line in text.splitlines(keepends=True):
        body, comment = line, ""
        if not is_json:
            quote = None
            for index, character in enumerate(line):
                if character in "'\"":
                    if quote == character:
                        quote = None
                    elif quote is None and (index == 0 or line[index - 1].isspace()
                                            or line[index - 1] in ":=,[{-"):
                        quote = character
                elif character == "#" and quote is None \
                        and (index == 0 or line[index - 1].isspace()):
                    body, comment = line[:index], line[index:]
                    break
        for legacy, target in replacements.items():
            if is_json:
                pattern = rf'(?P<prefix>:\s*["\']){re.escape(legacy)}(?P<suffix>["\'])'
            elif is_env:
                pattern = (
                    rf'(?P<prefix>^(?:export\s+)?[A-Z0-9_]*MODEL[A-Z0-9_]*=\s*)'
                    rf'(?:(?P<double>"){re.escape(legacy)}"|'
                    rf"(?P<single>'){re.escape(legacy)}'|(?P<bare>{re.escape(legacy)}))"
                    rf'(?=\s*(?:#\s.*)?$)'
                )
            else:
                # YAML active configuration includes both ordinary ``model: value`` scalars and
                # nested mappings such as ``model: {default: value}``. Match an exact mapping
                # value, rather than the token globally, so profile keys and @profile references
                # retain their identity.
                pattern = (
                    rf'(?P<prefix>\b(?:model|default)\s*:\s*)'
                    rf'(?:(?P<double>"){re.escape(legacy)}"|'
                    rf"(?P<single>'){re.escape(legacy)}'|(?P<bare>{re.escape(legacy)}))"
                    rf'(?=\s*(?:[,}}\]]|#\s.*|$))'
                )
            body, changed = re.subn(
                pattern,
                lambda match: (
                    match.group("prefix")
                    + ('"' if match.groupdict().get("double") else
                       "'" if match.groupdict().get("single") else "")
                    + target
                    + ('"' if match.groupdict().get("double") else
                       "'" if match.groupdict().get("single") else "")
                    + match.groupdict().get("suffix", "")
                ),
                body,
                flags=re.IGNORECASE,
            )
            count += changed
        lines.append(body + comment)
    return "".join(lines), count


def apply(pack: Path, dry_run: bool) -> list[str]:
    changes: list[str] = []
    for relative in _active_config_paths(pack):
        path = pack / relative
        if not path.is_file():
            continue
        before = path.read_text(encoding="utf-8")
        after, replaced = _replace_active_values(relative, before)
        if replaced == 0:
            continue
        changes.append(f"set {replaced} DeepSeek model selection(s) in {relative} to {_TARGET_ID}")
        if not dry_run:
            path.write_text(after, encoding="utf-8")
    return changes
