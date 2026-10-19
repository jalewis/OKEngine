"""Retired actor eligibility for generated active navigation; no corpus mutation."""
from pathlib import Path

import okf_migrate
import schema_lib
from okengine import actor_types  # schema_lib has put the engine package on sys.path
# The whole-envelope reader every generator shares (okengine#991 audit 2 [2], okengine#993); kept
# importable from here for the generators that already use these names.
from frontmatter_lib import FRONTMATTER_RE, frontmatter_block, read_frontmatter  # noqa: F401


def is_retired_actor(fm: dict | None, root: Path, page: Path) -> bool:
    """Exclude only explicitly tombstoned actors, of every actor type the schema declares
    (okengine#1020) and its type aliases."""
    if not isinstance(fm, dict) or not okf_migrate.is_tombstoned(fm):
        return False
    namespace = page.parent.relative_to(root / "wiki").as_posix()
    schema = schema_lib.governing_schema(root, namespace)
    return actor_types.is_actor_type(schema, fm.get("type"))
