"""Copy and restore the files a running queue needs after a disk failure.

The set is small: SQLite, operator settings, secrets, the model registry, and
the audit log. Destination is a directory the operator owns; nothing is sent
off the machine.
"""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from product.model_store import model_path, models_dir
from product.ops import ops_path, secrets_path
from product.settings import settings_path
from product.store import DEFAULT_DB


def runtime_paths() -> dict[str, Path]:
    """Names stored in the backup folder → live paths."""
    model = model_path()
    paths = {
        "queue.db": Path(os.environ.get("QUEUE_DB") or DEFAULT_DB),
        "ops.json": ops_path(),
        "secrets.json": secrets_path(),
        "settings.json": settings_path(),
        "model.pkl": model,
        "model.pkl.sha256": model.with_name(model.name + ".sha256"),
        "models": models_dir(),
    }
    audit = os.environ.get("AUDIT_LOG_PATH")
    if audit is None or audit.strip().lower() not in {"", "0", "off", "-"}:
        paths["audit.log"] = Path(audit.strip()) if audit and audit.strip() else ops_path().parent / "audit.log"
    return paths


def backup(dest: str | Path) -> list[str]:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name, src in runtime_paths().items():
        if not src.exists():
            continue
        target = dest / name
        if src.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
        else:
            shutil.copy2(src, target)
        copied.append(name)
    (dest / "MANIFEST.txt").write_text("\n".join(copied) + "\n", encoding="utf-8")
    return copied


def restore(src: str | Path) -> list[str]:
    src = Path(src)
    if not src.is_dir():
        raise FileNotFoundError(f"backup folder not found: {src}")
    restored: list[str] = []
    for name, dest in runtime_paths().items():
        item = src / name
        if not item.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)
        restored.append(name)
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy or restore queue.db, ops.json, models, and the audit log.")
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("folder", help="Directory to write (backup) or read (restore)")
    args = parser.parse_args(argv)
    names = backup(args.folder) if args.action == "backup" else restore(args.folder)
    print("\n".join(names) if names else "(nothing to copy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
