"""Test NFS file generator: produces random files to simulate NFS ingest."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

NFS_INCOMING_DIR = Path("/mnt/nfs/incoming")

app = FastAPI()


@dataclass
class GeneratorState:
    active: bool = False
    session_name: str | None = None
    interval_s: float = 0.0
    file_size_bytes: int = 0
    file_count: int = 0
    files_generated: int = 0
    task: asyncio.Task[None] | None = field(default=None, repr=False)


state = GeneratorState()


class GenerateStartRequest(BaseModel):
    session_name: str
    interval_s: float = 2.0
    file_size_bytes: int = 65536
    file_count: int = 10


class GenerateStartResponse(BaseModel):
    session_name: str
    interval_s: float
    file_size_bytes: int
    file_count: int


class GenerateStopResponse(BaseModel):
    active: bool
    files_generated: int


class GenerateStatusResponse(BaseModel):
    active: bool
    session_name: str | None
    interval_s: float
    file_size_bytes: int
    file_count: int
    files_generated: int


class HealthResponse(BaseModel):
    ok: bool = True


async def _generate_files() -> None:
    """Background task: write random files at the configured interval."""
    session_dir = NFS_INCOMING_DIR / state.session_name  # type: ignore[operator]
    os.makedirs(session_dir, exist_ok=True)

    while state.file_count == 0 or state.files_generated < state.file_count:
        filename = f"file-{state.files_generated:04d}-{uuid4().hex[:8]}.bin"
        filepath = session_dir / filename
        data = os.urandom(state.file_size_bytes)
        filepath.write_bytes(data)
        state.files_generated += 1
        logger.info(
            "wrote %s (%d bytes) [%d/%s]",
            filename,
            state.file_size_bytes,
            state.files_generated,
            state.file_count or "unlimited",
        )
        await asyncio.sleep(state.interval_s)

    logger.info("generation complete: %d files", state.files_generated)
    state.active = False


@app.post("/v1/generate/start")
async def generate_start(body: GenerateStartRequest) -> GenerateStartResponse:
    """Start generating test files into the session directory."""
    if state.active:
        raise HTTPException(status_code=409, detail="Generation already active")

    state.active = True
    state.session_name = body.session_name
    state.interval_s = body.interval_s
    state.file_size_bytes = body.file_size_bytes
    state.file_count = body.file_count
    state.files_generated = 0
    state.task = asyncio.create_task(_generate_files())

    return GenerateStartResponse(
        session_name=body.session_name,
        interval_s=body.interval_s,
        file_size_bytes=body.file_size_bytes,
        file_count=body.file_count,
    )


@app.post("/v1/generate/stop")
async def generate_stop() -> GenerateStopResponse:
    """Stop the active file generation."""
    if state.task and not state.task.done():
        state.task.cancel()
        try:
            await state.task
        except asyncio.CancelledError:
            pass
    generated = state.files_generated
    state.active = False
    state.task = None
    return GenerateStopResponse(active=False, files_generated=generated)


@app.get("/v1/generate/status")
async def generate_status() -> GenerateStatusResponse:
    """Return current generator state."""
    return GenerateStatusResponse(
        active=state.active,
        session_name=state.session_name,
        interval_s=state.interval_s,
        file_size_bytes=state.file_size_bytes,
        file_count=state.file_count,
        files_generated=state.files_generated,
    )


@app.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse()
