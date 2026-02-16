"""Session state machine: naming, validation, directory creation, lifecycle."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from anyio.to_thread import run_sync
from uuid_utils import uuid7

from app.config import Settings


@dataclass
class SessionState:
    """Mutable session state shared across the application."""

    active: bool = False
    session_name: str | None = None
    date_prefix: str | None = None
    processed_ok: int = 0
    processed_err: int = 0
    last_error: str | None = None


def generate_session_name() -> str:
    """Generate an auto-session name using UUIDv7."""
    return f"00-session-{uuid7()}"


async def start_session(
    session_state: SessionState,
    settings: Settings,
    session_name: str | None,
) -> tuple[str, str]:
    """Start a new session. Returns (date_prefix, session_name).

    Raises ValueError if a session is already active.
    """
    if session_state.active:
        raise ValueError("Session already active")

    name = session_name or generate_session_name()
    date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")

    # Create directory trees
    incoming_dir = settings.nfs_incoming_dir / name
    processing_dir = settings.nfs_processing_root / date_prefix / name
    staging_dir = settings.local_staging_root / date_prefix / name

    await run_sync(
        lambda: os.makedirs(incoming_dir, exist_ok=True),
        abandon_on_cancel=True,
    )
    await run_sync(
        lambda: os.makedirs(processing_dir, exist_ok=True),
        abandon_on_cancel=True,
    )
    os.makedirs(staging_dir, exist_ok=True)

    session_state.active = True
    session_state.session_name = name
    session_state.date_prefix = date_prefix

    return date_prefix, name


def stop_session(session_state: SessionState) -> None:
    """Stop the active session. Name and date_prefix preserved for draining workers."""
    session_state.active = False
