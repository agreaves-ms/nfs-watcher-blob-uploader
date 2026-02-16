"""Worker pool: claim -> copy -> upload -> mark completed."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import time
import traceback
from pathlib import Path

from anyio.to_thread import run_sync
from azure.storage.blob.aio import ContainerClient

from app.azure_client import upload_file
from app.config import Settings
from app.models import WorkItem
from app.session import SessionState
from app.telemetry import (
    file_size_hist,
    files_failed,
    files_processed,
    queue_depth,
    upload_duration,
)

logger = logging.getLogger(__name__)


def _copy_with_fsync(src: Path, dst: Path) -> None:
    """Copy file preserving metadata and fsync the destination."""
    shutil.copy2(src, dst)
    fd = os.open(dst, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def worker(
    worker_id: int,
    queue: asyncio.Queue[WorkItem],
    container_client: ContainerClient,
    session_state: SessionState,
    settings: Settings,
) -> None:
    """Consume WorkItems from the queue and process them."""
    while True:
        item = await queue.get()
        try:
            await _process_item(item, container_client, session_state, settings)
            session_state.processed_ok += 1
            files_processed.add(1)
        except Exception:
            session_state.processed_err += 1
            session_state.last_error = f"{item.filename}: {traceback.format_exc()}"
            files_failed.add(1)
            logger.exception(
                "Failed to process %s",
                item.filename,
                extra={"file_name": item.filename, "session_name": item.session_name},
            )
        finally:
            queue.task_done()
            queue_depth.add(-1)


async def _process_item(
    item: WorkItem,
    container_client: ContainerClient,
    session_state: SessionState,
    settings: Settings,
) -> None:
    """Execute the per-file pipeline: claim, copy, upload, mark completed."""
    processing_dir = settings.nfs_processing_root / item.date_prefix / item.session_name
    processing_path = processing_dir / item.filename
    staging_dir = settings.local_staging_root / item.date_prefix / item.session_name
    staging_path = staging_dir / item.filename
    blob_name = f"{item.date_prefix}/{item.session_name}/{item.filename}"

    # 1. Claim (skip if recovery — file already in .processing)
    if not item.from_recovery:
        try:
            await run_sync(
                lambda: os.rename(item.source_path, processing_path),
                abandon_on_cancel=True,
            )
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESTALE):
                logger.debug("File already claimed: %s", item.filename)
                return
            raise

    # 2. Copy to local staging
    await run_sync(
        lambda: os.makedirs(staging_dir, exist_ok=True),
        abandon_on_cancel=True,
    )
    await run_sync(
        lambda: _copy_with_fsync(processing_path, staging_path),
        abandon_on_cancel=True,
    )

    # 3. Upload to Azure
    start = time.monotonic()
    file_size = staging_path.stat().st_size
    await upload_file(
        container_client,
        staging_path,
        blob_name,
        max_concurrency=settings.azure_max_concurrency,
    )
    duration = time.monotonic() - start
    upload_duration.record(duration)
    file_size_hist.record(file_size)
    logger.info(
        "Upload complete: %s",
        item.filename,
        extra={
            "file_name": item.filename,
            "session_name": item.session_name,
            "date_prefix": item.date_prefix,
            "blob_name": blob_name,
            "size_bytes": file_size,
            "duration_s": round(duration, 3),
        },
    )

    # 4. Mark completed + cleanup local staging
    completed_path = processing_path.with_name(processing_path.name + ".completed")
    await run_sync(
        lambda: os.rename(processing_path, completed_path),
        abandon_on_cancel=True,
    )
    try:
        await run_sync(
            lambda: os.unlink(staging_path),
            abandon_on_cancel=True,
        )
    except OSError:
        logger.warning("Could not delete staging file %s", staging_path, exc_info=True)
