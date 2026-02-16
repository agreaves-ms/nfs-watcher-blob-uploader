"""Startup scan of .processing/ and session auto-resume."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from anyio.to_thread import run_sync

from app.config import Settings
from app.models import WorkItem
from app.session import SessionState
from app.telemetry import queue_depth

logger = logging.getLogger(__name__)


async def recover(
    queue: asyncio.Queue[WorkItem],
    session_state: SessionState,
    settings: Settings,
) -> int:
    """Scan .processing/ and re-enqueue unfinished files.

    Returns the number of files re-enqueued. Sets session_state if a
    recoverable session is found.
    """

    def _scan_processing() -> list[WorkItem]:
        items: list[WorkItem] = []
        root = settings.nfs_processing_root
        if not root.exists():
            return items
        for date_dir in sorted(root.iterdir(), reverse=False):
            if not date_dir.is_dir():
                continue
            date_prefix = date_dir.name
            for session_dir in sorted(date_dir.iterdir(), reverse=False):
                if not session_dir.is_dir():
                    continue
                session_name = session_dir.name
                for entry in os.scandir(session_dir):
                    if not entry.is_file():
                        continue
                    if entry.name.endswith(".completed"):
                        continue
                    items.append(
                        WorkItem(
                            source_path=Path(entry.path),
                            session_name=session_name,
                            date_prefix=date_prefix,
                            filename=entry.name,
                            from_recovery=True,
                        )
                    )
        return items

    items = await run_sync(_scan_processing, abandon_on_cancel=True)

    if not items:
        return 0

    # Determine most recent session for auto-resume
    last_item = max(items, key=lambda i: (i.date_prefix, i.session_name))
    session_state.active = True
    session_state.session_name = last_item.session_name
    session_state.date_prefix = last_item.date_prefix

    for item in items:
        await queue.put(item)
        queue_depth.add(1)

    logger.info(
        "Recovery: re-enqueued %d files, resuming session %s/%s",
        len(items),
        last_item.date_prefix,
        last_item.session_name,
    )
    return len(items)
