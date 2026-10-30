"""Build the distributable ZIP (runnable files only).

Excludes the user's private live database (data/agent.db, data/backups/*) and
Python caches. A fresh deployment creates or upgrades its own database on first
launch: `app/db.py` runs the idempotent migration itself (no separate migrate
script is shipped).
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "bionic-agent-mcp.zip"

INCLUDE_DIRS = ["app", "dsh-panel-client"]
INCLUDE_FILES = [
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    ".env.example",
    "deepseek-harness-mcp.example.yml",
]
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".bak")
EXCLUDE_DIRS = {"__pycache__", "backups", ".git", "node_modules"}


def iter_files():
    seen = set()
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts):
                continue
            if p.is_dir():
                continue
            if p.suffix in EXCLUDE_SUFFIXES:
                continue
            seen.add(p)
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.exists():
            seen.add(p)
    return sorted(seen)


def main():
    files = iter_files()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            arcname = p.relative_to(ROOT).as_posix()
            z.write(p, arcname)
            print("  +", arcname)
    size = OUT.stat().st_size
    print(f"\nWrote {OUT.name} ({size:,} bytes, {len(files)} files)")


if __name__ == "__main__":
    main()
