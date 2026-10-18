"""Database-free structure checks; not a substitute for Alembic check on PostgreSQL."""

import ast
import importlib
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from huipi_cloud.infrastructure.database.base import Base

ROOT = Path(__file__).resolve().parents[1]


def check() -> dict:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    if len(heads) != 1:
        raise RuntimeError("expected exactly one migration head")
    revisions = list(scripts.walk_revisions())  # Resolves ancestors; no env.py/DB.
    if len(scripts.get_bases()) != 1 or any(r.branch_labels for r in revisions):
        raise RuntimeError("expected a single unbranched migration history")
    if any(len(r._normalized_down_revisions) > 1 for r in revisions):
        raise RuntimeError("unexpected migration merge")
    declared = set()
    for revision in revisions:
        tree = ast.parse(Path(revision.path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_table"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                declared.add(node.args[0].value)
    for path in sorted((ROOT / "src/huipi_cloud/modules").rglob("*models.py")):
        module = ".".join(path.relative_to(ROOT / "src").with_suffix("").parts)
        importlib.import_module(module)
    tables = Base.metadata.sorted_tables  # Resolves mapped FK targets without I/O.
    missing = set(Base.metadata.tables) - declared
    if missing:
        raise RuntimeError(f"mapped tables without create_table migration: {sorted(missing)}")
    return {"head": heads[0], "revisions": len(revisions), "mapped_tables": len(tables)}


if __name__ == "__main__":
    print(check())
