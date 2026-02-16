"""FastAPI app, lifespan, and route wiring."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request

from app.azure_client import close_azure_client, create_azure_client
from app.config import Settings
from app.gc import gc_loop
from app.models import (
    HealthResponse,
    ReadyResponse,
    StatusResponse,
    WatchStartRequest,
    WatchStartResponse,
    WatchStopResponse,
    WorkItem,
)
from app.recovery import recover
from app.session import SessionState, start_session, stop_session
from app.telemetry import setup_telemetry
from app.watcher import watcher_loop
from app.worker import worker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()  # type: ignore[call-arg]  # pydantic-settings reads from env

    # 1. Telemetry (first up, last down)
    tracer_provider, meter_provider = setup_telemetry(app)

    # 2. Azure client (fail-fast auth + container validation)
    blob_service_client, container_client, credential = await create_azure_client(
        settings
    )

    # 3. Session state
    session_state = SessionState()

    # 4. Work queue
    queue: asyncio.Queue[WorkItem] = asyncio.Queue(maxsize=settings.max_queue_size)

    # 5. Recovery (scan .processing, auto-resume session)
    await recover(queue, session_state, settings)

    # 6. Background tasks
    gc_task = asyncio.create_task(gc_loop(settings))
    worker_tasks = [
        asyncio.create_task(worker(i, queue, container_client, session_state, settings))
        for i in range(settings.worker_concurrency)
    ]
    watcher_task = asyncio.create_task(watcher_loop(queue, session_state, settings))

    # Store on app.state for endpoint access
    app.state.settings = settings
    app.state.session = session_state
    app.state.queue = queue
    app.state.container_client = container_client
    app.state.ready = True

    yield

    # Shutdown: cancel background tasks
    watcher_task.cancel()
    for t in worker_tasks:
        t.cancel()
    gc_task.cancel()
    await asyncio.gather(watcher_task, *worker_tasks, gc_task, return_exceptions=True)

    # Close Azure resources
    await close_azure_client(blob_service_client, credential)

    # Shut down telemetry last
    tracer_provider.shutdown()
    meter_provider.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> HealthResponse:
    """Liveness probe — pure async, no NFS access."""
    return HealthResponse()


@app.get("/readyz")
async def readyz(request: Request) -> ReadyResponse:
    """Readiness probe — returns 503 until lifespan completes."""
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(status_code=503, detail="Not ready")
    return ReadyResponse()


@app.post("/v1/watch/start")
async def watch_start(request: Request, body: WatchStartRequest) -> WatchStartResponse:
    """Start a new watch session."""
    session_state: SessionState = request.app.state.session
    settings: Settings = request.app.state.settings

    if session_state.active:
        raise HTTPException(status_code=409, detail="Session already active")

    date_prefix, session_name = await start_session(
        session_state, settings, body.session_name
    )
    return WatchStartResponse(
        date_prefix=date_prefix,
        session_name=session_name,
        encoded_session=session_name,
    )


@app.post("/v1/watch/stop")
async def watch_stop(request: Request) -> WatchStopResponse:
    """Stop the active watch session. Workers continue draining the queue."""
    stop_session(request.app.state.session)
    return WatchStopResponse(enabled=False)


@app.get("/v1/status")
async def status(request: Request) -> StatusResponse:
    """Return current session state and processing counters."""
    s: SessionState = request.app.state.session
    return StatusResponse(
        enabled=s.active,
        active_session=s.session_name,
        processed_ok=s.processed_ok,
        processed_err=s.processed_err,
        last_error=s.last_error,
    )


def run() -> None:
    """Entry point for the `nfs-watcher-uploader` console script."""
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
