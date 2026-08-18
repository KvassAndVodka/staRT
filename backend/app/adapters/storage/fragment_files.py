"""Power-loss-aware publication and reconciliation for PCM fragment files."""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Iterable


class FragmentPublicationError(Exception):
    pass


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_pcm_fragment(
    final_path: Path,
    data: bytes,
    *,
    bytes_per_sample: int = 2,
) -> str:
    """Publish bytes at a final path only after file and directory durability barriers."""
    if not data:
        raise FragmentPublicationError("Refusing to publish an empty PCM fragment")
    if bytes_per_sample <= 0 or len(data) % bytes_per_sample != 0:
        raise FragmentPublicationError(
            f"PCM fragment byte count {len(data)} is not aligned to {bytes_per_sample}"
        )

    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists() or final_path.is_symlink():
        raise FragmentPublicationError(f"Fragment path already exists: {final_path.name}")

    temporary = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256(data).hexdigest()
    try:
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if not verify_fragment_file(
            temporary,
            expected_size=len(data),
            expected_sha256=digest,
        ):
            raise FragmentPublicationError(
                f"Staged fragment verification failed: {final_path.name}"
            )
        # A single capture producer owns this directory. The explicit existence
        # check prevents a retry from replacing a previously published fragment.
        if final_path.exists() or final_path.is_symlink():
            raise FragmentPublicationError(f"Fragment path already exists: {final_path.name}")
        os.replace(temporary, final_path)
        _fsync_directory(final_path.parent)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return digest


def verify_fragment_file(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == expected_sha256


def quarantine_fragment(path: Path, reason: str) -> Path:
    quarantine_dir = path.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / f"{path.name}.{reason}.{uuid.uuid4().hex}.quarantine"
    os.replace(path, target)
    _fsync_directory(path.parent)
    _fsync_directory(quarantine_dir)
    return target


def reconcile_unreferenced_fragment_files(
    sessions_root: Path,
    referenced_paths: Iterable[Path],
) -> tuple[list[Path], list[Path]]:
    """Remove abandoned staging files and quarantine final files without ledger rows."""
    referenced = {os.path.abspath(path) for path in referenced_paths}
    removed_temporary: list[Path] = []
    quarantined: list[Path] = []
    for fragment_dir in sessions_root.glob("*/fragments"):
        if not fragment_dir.is_dir():
            continue
        directory_changed = False
        for path in fragment_dir.iterdir():
            if path.is_dir():
                continue
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                path.unlink()
                removed_temporary.append(path)
                directory_changed = True
            elif path.suffix == ".raw" and os.path.abspath(path) not in referenced:
                quarantined.append(quarantine_fragment(path, "orphan"))
                directory_changed = True
        if directory_changed:
            _fsync_directory(fragment_dir)
    return removed_temporary, quarantined
