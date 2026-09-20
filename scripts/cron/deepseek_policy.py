"""Shared active-configuration inventory for DeepSeek migration and prevention."""
from pathlib import Path
import re
import shlex


ACTIVE_CONFIGS = tuple(Path(path) for path in (
    ".okengine/model-profiles.yaml", ".okengine/cron-models.json",
    ".okengine/extension-models.json", "crons/domain-crons.json",
    ".hermes-data/config.yaml", ".hermes-data/.env", ".env", ".hermes/.env", ".hermes/config.yaml",
    ".hermes/config.yml", "config.yaml", "config.yml",
))
LEGACY_MODELS = frozenset(
    prefix + model
    for prefix in ("", "deepseek/", "openrouter/deepseek/")
    for model in ("deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash",
                  "deepseek-v4-flash-vision-exp", "deepseek-v4-pro")
)
MODEL_ENV_KEY = re.compile(r"[A-Z0-9_]*MODEL[A-Z0-9_]*\Z")


def legacy_deepseek_model(value):
    model = str(value or "").strip().lower()
    return model if model in LEGACY_MODELS else None


def model_values(node):
    """Model/default scalars and model environment assignments, never prose or keys."""
    if isinstance(node, dict):
        for key, value in node.items():
            if (key in {"model", "default"} or MODEL_ENV_KEY.fullmatch(str(key))) \
                    and not isinstance(value, (dict, list)):
                yield value
            else:
                yield from model_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from model_values(value)


def active_config_paths(pack: Path) -> list[Path]:
    relative = set(ACTIVE_CONFIGS)
    for root in (Path("extensions"), Path(".okengine/extensions")):
        base = pack / root
        if base.is_dir():
            relative.update(path.relative_to(pack) for path in base.rglob("extension.yaml"))
            relative.update(path.relative_to(pack) for path in base.rglob("*.cron.json"))
    return sorted(relative)


def env_model_values(text: str):
    """Read model assignments without evaluating shell expressions or exposing other values."""
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if match and MODEL_ENV_KEY.fullmatch(match[1]):
            values = shlex.split(match[2], comments=True)
            yield match[1], " ".join(values)
