"""NFS polling loop: scans incoming directory and enqueues stable files."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
from pathlib import Path

from anyio.to_thread import run_sync

from app.config import Settings
from app.models import WorkItem
from app.session import SessionState
from app.telemetry import queue_depth

logger = logging.getLogger(__name__)

ScanMap = dict[str, tuple[int, float]]


async def watcher_loop(
    queue: asyncio.Queue[WorkItem],
    session_state: SessionState,
    settings: Settings,
) -> None:
    """Poll NFS incoming directory and enqueue stable files."""
    previous: ScanMap = {}
    pending: set[str] = set()
    backoff = 0.0
    allowed_extensions = settings.file_extensions or None

    while True:
        await asyncio.sleep(settings.poll_interval_s + backoff)

        if not session_state.active:
            previous = {}
            pending.clear()
            continue

        incoming_dir = settings.nfs_incoming_dir / session_state.session_name  # type: ignore[operator]
        try:
            current = await _scan_directory(incoming_dir, allowed_extensions)
            backoff = 0.0
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESTALE):
                logger.warning(
                    "NFS scan: %s (errno=%s), skipping cycle", exc, exc.errno
                )
                continue
            logger.error("NFS scan error: %s", exc)
            backoff = min(backoff * 2 or 1.0, 60.0)
            continue

        # Prune pending entries for files no longer in incoming (worker claimed them)
        pending -= pending - set(current.keys())

        now = time.time()
        for filename, (size, mtime) in current.items():
            if filename in pending:
                continue
            prev = previous.get(filename)
            if prev is None:
                continue
            prev_size, prev_mtime = prev
            if size != prev_size or mtime != prev_mtime:
                continue
            if (now - mtime) < settings.min_file_age_s:
                continue

            item = WorkItem(
                source_path=incoming_dir / filename,
                session_name=session_state.session_name,  # type: ignore[arg-type]
                date_prefix=session_state.date_prefix,  # type: ignore[arg-type]
                filename=filename,
                from_recovery=False,
            )
            await queue.put(item)
            pending.add(filename)
            queue_depth.add(1)

        previous = current


async def _scan_directory(
    path: Path,
    allowed_extensions: frozenset[str] | None,
) -> ScanMap:
    """Scan a directory for files, returning name->(size, mtime) map."""

    def _scan() -> ScanMap:
        result: ScanMap = {}
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if (
                        allowed_extensions
                        and Path(entry.name).suffix.lower() not in allowed_extensions
                    ):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        result[entry.name] = (stat.st_size, stat.st_mtime)
                    except OSError as exc:
                        if exc.errno in (errno.ENOENT, errno.ESTALE):
                            continue
                        raise
        except FileNotFoundError:
            pass
        return result

    return await run_sync(_scan, abandon_on_cancel=True)
