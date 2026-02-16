"""Background GC for .completed files in .processing/."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from anyio.to_thread import run_sync

from app.config import Settings

logger = logging.getLogger(__name__)


async def gc_loop(settings: Settings) -> None:
    """Periodically sweep .completed files from .processing/ and prune empty dirs."""
    while True:
        await asyncio.sleep(settings.gc_interval_s)
        try:
            await _gc_sweep(settings)
        except Exception:
            logger.warning("GC sweep failed", exc_info=True)


async def _gc_sweep(settings: Settings) -> None:
    """Delete .completed files from NFS and corresponding staging files."""

    def _sweep() -> list[Path]:
        completed: list[Path] = []
        processing_root = settings.nfs_processing_root
        if not processing_root.exists():
            return completed
        for dirpath, _, filenames in os.walk(processing_root):
            for name in filenames:
                if name.endswith(".completed"):
                    completed.append(Path(dirpath) / name)
        return completed

    completed_files = await run_sync(_sweep, abandon_on_cancel=True)

    for nfs_path in completed_files:
        try:
            await run_sync(
                lambda p=nfs_path: os.unlink(p),  # type: ignore[misc]
                abandon_on_cancel=True,
            )
            # Safety-net: delete staging file if the worker didn't
            relative = nfs_path.relative_to(settings.nfs_processing_root)
            original_name = relative.name.removesuffix(".completed")
            staging_path = settings.local_staging_root / relative.parent / original_name
            staging_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("GC: could not delete %s", nfs_path, exc_info=True)

    await _prune_empty_dirs(settings.nfs_processing_root)


async def _prune_empty_dirs(root: Path) -> None:
    """Walk bottom-up and remove empty directories under root."""

    def _prune() -> None:
        if not root.exists():
            return
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            path = Path(dirpath)
            if path == root:
                continue
            if not filenames and not dirnames:
                try:
                    os.rmdir(path)
                except OSError:
                    pass

    await run_sync(_prune, abandon_on_cancel=True)
