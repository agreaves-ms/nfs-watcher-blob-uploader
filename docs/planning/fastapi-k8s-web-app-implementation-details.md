# Implementation Details: NFS Watch FastAPI K8s Web App

> Companion to the [implementation plan](./fastapi-k8s-web-app-implementation-plan.md).
> Contains function signatures, data structures, error handling, and
> module-level design for each phase.

---

## 1. Project Scaffolding

### pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "nfs-watcher-uploader"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "azure-storage-blob>=12.23.0",
    "azure-identity>=1.17.0",
    "uuid-utils>=0.9.0",
    "opentelemetry-api>=1.27.0",
    "opentelemetry-sdk>=1.27.0",
    "opentelemetry-exporter-otlp-proto-http>=1.27.0",
    "opentelemetry-instrumentation-fastapi>=0.48b0",
    "anyio>=4.0.0",
    "pydantic-settings>=2.0.0",
]

[project.scripts]
nfs-watcher-uploader = "app.main:run"

[project.optional-dependencies]
dev = [
    "ruff>=0.8.0",
    "pyright>=1.1.0",
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
]
```

### .gitignore

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
dist/
build/
*.egg
.venv/
venv/

# IDE
.vscode/
.idea/
*.swp
*.swo

# Environment
.env

# Local dev data directories
data/

# OS
.DS_Store
Thumbs.db
```

### .env.example

```bash
# Azure — uses Azurite emulator locally
APP_AZURE_ACCOUNT_URL=http://127.0.0.1:10000/devstoreaccount1
APP_AZURE_CONTAINER=ingest
APP_AZURE_CONNECTION_STRING=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;

# NFS — local directories for development
APP_NFS_INCOMING_DIR=./data/incoming
APP_NFS_PROCESSING_ROOT=./data/.processing
APP_LOCAL_STAGING_ROOT=./data/staging

# Watcher tuning — faster for dev
APP_POLL_INTERVAL_S=1.0
APP_MIN_FILE_AGE_S=2.0

# Workers
APP_WORKER_CONCURRENCY=2

# OTel — comment out to disable telemetry
# OTEL_SERVICE_NAME=nfs-watcher-uploader
# OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

### Dockerfile

```dockerfile
# Stage 1: dependencies
FROM python:3.12-slim-bookworm AS deps
WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --target=/deps .

# Stage 2: runtime
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=deps /deps /usr/local/lib/python3.12/site-packages
COPY app/ ./app/
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Multi-arch: build natively on Jetson (`docker build`) or cross-compile with
`docker buildx --platform linux/amd64,linux/arm64`.

---

## 2. Configuration

### `app/config.py`

```python
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "APP_"}

    # Azure (required)
    azure_account_url: str
    azure_container: str

    # Azure (optional fallback auth)
    azure_connection_string: str | None = None
    azure_account_name: str | None = None
    azure_account_key: str | None = None

    # NFS paths
    nfs_incoming_dir: Path = Path("/mnt/nfs/incoming")
    nfs_processing_root: Path = Path("/mnt/nfs/.processing")

    # Local staging
    local_staging_root: Path = Path("/mnt/staging")

    # Watcher tuning
    poll_interval_s: float = 2.0
    min_file_age_s: float = 5.0
    file_extensions: frozenset[str] = frozenset()

    # Queue and workers
    max_queue_size: int = 2000
    worker_concurrency: int = 4

    # Azure upload tuning (None = SDK defaults)
    azure_max_block_size: int | None = None
    azure_max_single_put_size: int | None = None
    azure_max_concurrency: int = 8

    # GC
    gc_interval_s: float = 30.0

    @field_validator("file_extensions", mode="before")
    @classmethod
    def parse_extensions(cls, v: str | frozenset[str]) -> frozenset[str]:
        """Parse comma-separated extensions: '.bin,.mp4,.dat' -> frozenset."""
        if isinstance(v, str):
            if not v.strip():
                return frozenset()
            return frozenset(
                ext.strip().lower() if ext.strip().startswith(".")
                else f".{ext.strip().lower()}"
                for ext in v.split(",")
                if ext.strip()
            )
        return v
```

### Env var reference

| Variable | Type | Default | Notes |
|----------|------|---------|-------|
| `APP_AZURE_ACCOUNT_URL` | str | *required* | `https://<acct>.blob.core.windows.net` |
| `APP_AZURE_CONTAINER` | str | *required* | Target blob container |
| `APP_AZURE_CONNECTION_STRING` | str | None | Fallback auth |
| `APP_AZURE_ACCOUNT_NAME` | str | None | Fallback auth (with key) |
| `APP_AZURE_ACCOUNT_KEY` | str | None | Fallback auth (with name) |
| `APP_NFS_INCOMING_DIR` | Path | `/mnt/nfs/incoming` | |
| `APP_NFS_PROCESSING_ROOT` | Path | `/mnt/nfs/.processing` | |
| `APP_LOCAL_STAGING_ROOT` | Path | `/mnt/staging` | |
| `APP_POLL_INTERVAL_S` | float | 2.0 | |
| `APP_MIN_FILE_AGE_S` | float | 5.0 | Must be >= NFS `actimeo` |
| `APP_FILE_EXTENSIONS` | str | "" (all) | `.bin,.mp4,.dat` |
| `APP_MAX_QUEUE_SIZE` | int | 2000 | |
| `APP_WORKER_CONCURRENCY` | int | 4 | |
| `APP_AZURE_MAX_BLOCK_SIZE` | int | None (SDK) | Bytes |
| `APP_AZURE_MAX_SINGLE_PUT_SIZE` | int | None (SDK) | Bytes |
| `APP_AZURE_MAX_CONCURRENCY` | int | 8 | Parallel block uploads |
| `APP_GC_INTERVAL_S` | float | 30.0 | |

---

## 3. Models & Types

### `app/models.py`

```python
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel, field_validator
import re


# --- Work queue item ---

@dataclass(frozen=True, slots=True)
class WorkItem:
    source_path: Path
    session_name: str
    date_prefix: str
    filename: str
    from_recovery: bool


# --- API request/response ---

SESSION_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")


class WatchStartRequest(BaseModel):
    session_name: str | None = None

    @field_validator("session_name")
    @classmethod
    def validate_session_name(cls, v: str | None) -> str | None:
        if v is not None and not SESSION_NAME_RE.match(v):
            raise ValueError(
                "session_name must match [a-zA-Z0-9_.-]"
            )
        return v


class WatchStartResponse(BaseModel):
    date_prefix: str
    session_name: str
    encoded_session: str


class WatchStopResponse(BaseModel):
    enabled: bool


class StatusResponse(BaseModel):
    enabled: bool
    active_session: str | None
    processed_ok: int
    processed_err: int
    last_error: str | None


class HealthResponse(BaseModel):
    ok: bool = True


class ReadyResponse(BaseModel):
    ready: bool = True
```

### WorkItem lifecycle

```
Watcher creates WorkItem(from_recovery=False):
  source_path = /mnt/nfs/incoming/{{session}}/file.dat

Recovery creates WorkItem(from_recovery=True):
  source_path = /mnt/nfs/.processing/YYYYMMDD/{{session}}/file.dat

Worker reads from_recovery to decide whether to claim (rename).
```

---

## 4. Telemetry

### `app/telemetry.py`

**Initialization function** called early in lifespan:

```python
def setup_telemetry(app: FastAPI) -> tuple[TracerProvider, MeterProvider]:
    ...
```

**TracerProvider setup**:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
```

**MeterProvider setup**:

```python
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

reader = PeriodicExportingMetricReader(OTLPMetricExporter())
meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
metrics.set_meter_provider(meter_provider)
```

**Custom metrics**:

```python
meter = metrics.get_meter("nfs-watcher-uploader")

files_processed = meter.create_counter("files.processed")
files_failed = meter.create_counter("files.failed")
upload_duration = meter.create_histogram("upload.duration", unit="s")
file_size_hist = meter.create_histogram("file.size", unit="By")
queue_depth = meter.create_up_down_counter("queue.depth")
```

**FastAPI instrumentation**:

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
```

**JSON structured logging**:

Custom `logging.Formatter` subclass that outputs JSON with fields:
`timestamp`, `level`, `message`, `logger`, `trace_id`, `span_id`,
`trace_flags`, plus any extra fields (`file_name`, `session_name`).

Trace context extracted from `opentelemetry.trace.get_current_span()`.

**Logger quieting**:

```python
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
```

---

## 5. Azure Client

### `app/azure_client.py`

**Lifecycle** (called from lifespan):

```python
async def create_azure_client(
    settings: Settings,
) -> tuple[BlobServiceClient, ContainerClient, AsyncTokenCredential]:
    """Create and validate Azure client. Raises SystemExit on failure."""
    ...
```

**Auth chain**:

`DefaultAzureCredential()` does not fail at construction — failure occurs on
first use. The auth flow wraps the container validation step:

```python
from azure.identity.aio import DefaultAzureCredential
from azure.core.exceptions import ClientAuthenticationError

credential = DefaultAzureCredential()
blob_service_client = BlobServiceClient(
    account_url=settings.azure_account_url,
    credential=credential,
    max_block_size=settings.azure_max_block_size,       # None = SDK default
    max_single_put_size=settings.azure_max_single_put_size,  # None = SDK default
)

try:
    container_client = blob_service_client.get_container_client(settings.azure_container)
    await container_client.get_container_properties()
except ClientAuthenticationError:
    await blob_service_client.close()
    await credential.close()
    # Attempt fallback
    blob_service_client, container_client, credential = _try_fallback(settings)
```

If `DefaultAzureCredential` fails AND `azure_connection_string` or
`(azure_account_name, azure_account_key)` are set, fall back:

```python
def _try_fallback(settings: Settings) -> ...:
    if settings.azure_connection_string:
        blob_service_client = BlobServiceClient.from_connection_string(
            settings.azure_connection_string,
        )
    elif settings.azure_account_name and settings.azure_account_key:
        blob_service_client = BlobServiceClient(
            account_url=settings.azure_account_url,
            credential=settings.azure_account_key,
        )
    else:
        raise SystemExit("No viable Azure credentials")
    ...
```

**Container validation**:

```python
container_client = blob_service_client.get_container_client(settings.azure_container)
try:
    await container_client.get_container_properties()
except ResourceNotFoundError:
    try:
        await container_client.create_container()
    except Exception as exc:
        await blob_service_client.close()
        await credential.close()
        raise SystemExit(f"Cannot access/create container: {exc}")
```

**Upload helper**:

`max_block_size` and `max_single_put_size` are set at `BlobServiceClient`
construction time (see auth chain above). Only `max_concurrency` is a
per-call parameter on `upload_blob()`.

```python
async def upload_file(
    container_client: ContainerClient,
    local_path: Path,
    blob_name: str,
    max_concurrency: int,
) -> None:
    """Upload a local file to Azure Blob Storage as Block Blob."""
    blob_client = container_client.get_blob_client(blob_name)
    file_size = local_path.stat().st_size
    with open(local_path, "rb") as f:
        await blob_client.upload_blob(
            f,
            overwrite=True,
            blob_type="BlockBlob",
            max_concurrency=max_concurrency,
            length=file_size,
        )
```

Note: `local_path.stat().st_size` is safe because the file is on local
ephemeral storage, not NFS.

**Shutdown** (called from lifespan):

```python
async def close_azure_client(
    blob_service_client: BlobServiceClient,
    credential: AsyncTokenCredential,
) -> None:
    await blob_service_client.close()
    await credential.close()
```

---

## 6. Session Management

### `app/session.py`

**State**:

```python
from dataclasses import dataclass, field


@dataclass
class SessionState:
    active: bool = False
    session_name: str | None = None
    date_prefix: str | None = None
    processed_ok: int = 0
    processed_err: int = 0
    last_error: str | None = None
```

A single `SessionState` instance lives on `app.state.session`.

**Session start**:

```python
from datetime import datetime, timezone
from uuid_utils import uuid7


def generate_session_name() -> str:
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

    # Create directories (NFS calls in thread)
    incoming_dir = settings.nfs_incoming_dir / name
    processing_dir = settings.nfs_processing_root / date_prefix / name
    staging_dir = settings.local_staging_root / date_prefix / name

    await anyio.to_thread.run_sync(
        lambda: os.makedirs(incoming_dir, exist_ok=True),
        abandon_on_cancel=True,
    )
    await anyio.to_thread.run_sync(
        lambda: os.makedirs(processing_dir, exist_ok=True),
        abandon_on_cancel=True,
    )
    os.makedirs(staging_dir, exist_ok=True)  # local, fast, no thread needed

    session_state.active = True
    session_state.session_name = name
    session_state.date_prefix = date_prefix

    return date_prefix, name
```

**Session stop**:

```python
def stop_session(session_state: SessionState) -> None:
    session_state.active = False
```

Session name and date_prefix are preserved so workers can finish.
Setting `active = False` tells the watcher to stop polling.

---

## 7. File Watcher

### `app/watcher.py`

**State**: two scan maps (current and previous), each mapping
`filename -> (size, mtime)`, plus a `pending` set of filenames already
enqueued but not yet claimed.

```python
ScanMap = dict[str, tuple[int, float]]
```

**Main loop**:

```python
async def watcher_loop(
    queue: asyncio.Queue[WorkItem],
    session_state: SessionState,
    settings: Settings,
) -> None:
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

        incoming_dir = settings.nfs_incoming_dir / session_state.session_name
        try:
            current = await _scan_directory(incoming_dir, allowed_extensions)
            backoff = 0.0
        except OSError as exc:
            logger.warning("NFS scan error: %s", exc)
            backoff = min(backoff * 2 or 1.0, 60.0)
            continue

        # Remove pending entries for files no longer in incoming (worker claimed them)
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
                session_name=session_state.session_name,
                date_prefix=session_state.date_prefix,
                filename=filename,
                from_recovery=False,
            )
            await queue.put(item)
            pending.add(filename)
            queue_depth.add(1)

        previous = current
```

**Directory scan** (offloaded to thread):

```python
async def _scan_directory(
    path: Path,
    allowed_extensions: frozenset[str] | None,
) -> ScanMap:
    def _scan() -> ScanMap:
        result: ScanMap = {}
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if allowed_extensions and Path(entry.name).suffix.lower() not in allowed_extensions:
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        result[entry.name] = (stat.st_size, stat.st_mtime)
                    except OSError as exc:
                        if exc.errno in (errno.ENOENT, errno.ESTALE):
                            continue
                        raise
        except FileNotFoundError:
            pass  # incoming dir may not exist yet
        return result

    return await anyio.to_thread.run_sync(_scan, abandon_on_cancel=True)
```

**Backpressure**: when the queue is full, `await queue.put(item)` blocks
the watcher. This is intentional: if workers can't keep up, the watcher
slows down.

**Pending set**: prevents double-enqueue when a scan cycle completes before
the worker claims the file via rename. Entries are pruned each cycle by
intersecting with the current scan results — when a file disappears from
incoming (the worker renamed it to `.processing`), it is removed from
`pending`.

---

## 8. Worker Pool

### `app/worker.py`

**Worker coroutine**:

```python
async def worker(
    worker_id: int,
    queue: asyncio.Queue[WorkItem],
    container_client: ContainerClient,
    session_state: SessionState,
    settings: Settings,
) -> None:
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
```

**Per-file pipeline**:

```python
async def _process_item(
    item: WorkItem,
    container_client: ContainerClient,
    session_state: SessionState,
    settings: Settings,
) -> None:
    processing_dir = settings.nfs_processing_root / item.date_prefix / item.session_name
    processing_path = processing_dir / item.filename
    staging_dir = settings.local_staging_root / item.date_prefix / item.session_name
    staging_path = staging_dir / item.filename
    blob_name = f"{item.date_prefix}/{item.session_name}/{item.filename}"

    # 1. Claim (skip if recovery)
    if not item.from_recovery:
        try:
            await anyio.to_thread.run_sync(
                lambda: os.rename(item.source_path, processing_path),
                abandon_on_cancel=True,
            )
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESTALE):
                logger.debug("File already claimed: %s", item.filename)
                return
            raise

    # 2. Copy to local staging
    await anyio.to_thread.run_sync(
        lambda: os.makedirs(staging_dir, exist_ok=True),
        abandon_on_cancel=True,
    )
    await anyio.to_thread.run_sync(
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

    # 4. Mark completed + cleanup local staging
    completed_path = processing_path.with_name(processing_path.name + ".completed")
    await anyio.to_thread.run_sync(
        lambda: os.rename(processing_path, completed_path),
        abandon_on_cancel=True,
    )
    await anyio.to_thread.run_sync(
        lambda: os.unlink(staging_path),
        abandon_on_cancel=True,
    )
```

**Copy helper**:

```python
def _copy_with_fsync(src: Path, dst: Path) -> None:
    shutil.copy2(src, dst)
    fd = os.open(dst, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
```

**Error handling per step**:

| Step | Failure | Behavior |
|------|---------|----------|
| Claim | `ENOENT` / `ESTALE` | Benign race; return (skip file) |
| Claim | Other `OSError` | Raise → file stays in incoming, rediscovered next scan |
| Copy | Any error | Raise → file stays in `.processing`, recovered on restart |
| Upload | Any error | Raise → file stays in `.processing`, recovered on restart |
| Mark completed | Any error | Raise → file stays in `.processing`, re-uploaded on restart (idempotent) |
| Delete staging | Any error | Log warning, continue (emptyDir is ephemeral) |

---

## 9. Garbage Collection

### `app/gc.py`

```python
async def gc_loop(settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.gc_interval_s)
        try:
            await _gc_sweep(settings)
        except Exception:
            logger.warning("GC sweep failed", exc_info=True)


async def _gc_sweep(settings: Settings) -> None:
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

    completed_files = await anyio.to_thread.run_sync(_sweep, abandon_on_cancel=True)

    for nfs_path in completed_files:
        try:
            # Delete NFS .completed file
            await anyio.to_thread.run_sync(
                lambda p=nfs_path: os.unlink(p),
                abandon_on_cancel=True,
            )
            # The worker already deleted the staging file after upload.
            # Derive and attempt cleanup as a safety net (missing_ok=True).
            relative = nfs_path.relative_to(settings.nfs_processing_root)
            original_name = relative.name.removesuffix(".completed")
            staging_path = settings.local_staging_root / relative.parent / original_name
            staging_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("GC: could not delete %s", nfs_path, exc_info=True)

    # Prune empty directories
    await _prune_empty_dirs(settings.nfs_processing_root)
```

**Directory pruning**: walk bottom-up, `os.rmdir()` on empty dirs.
`OSError` on non-empty dir is expected and caught.

---

## 10. Recovery

### `app/recovery.py`

```python
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
                    items.append(WorkItem(
                        source_path=Path(entry.path),
                        session_name=session_name,
                        date_prefix=date_prefix,
                        filename=entry.name,
                        from_recovery=True,
                    ))
        return items

    items = await anyio.to_thread.run_sync(_scan_processing, abandon_on_cancel=True)

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
```

**Auto-resume**: the most recent `(date_prefix, session_name)` tuple
(lexicographic max) becomes the active session. The watcher starts polling
for that session's incoming subfolder.

**Multiple sessions**: all files are re-enqueued regardless of which session
they belong to. The blob path is encoded in the `WorkItem`, so uploads land
in the correct location.

---

## 11. API Endpoints & Main App

### `app/main.py`

**Lifespan**:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()

    # 1. Telemetry
    tracer_provider, meter_provider = setup_telemetry(app)

    # 2. Azure client
    blob_service_client, container_client, credential = await create_azure_client(settings)

    # 3. Session state
    session_state = SessionState()

    # 4. Work queue
    queue: asyncio.Queue[WorkItem] = asyncio.Queue(maxsize=settings.max_queue_size)

    # 5. Recovery
    recovered = await recover(queue, session_state, settings)

    # 6. Background tasks
    gc_task = asyncio.create_task(gc_loop(settings))
    worker_tasks = [
        asyncio.create_task(
            worker(i, queue, container_client, session_state, settings)
        )
        for i in range(settings.worker_concurrency)
    ]
    watcher_task = asyncio.create_task(
        watcher_loop(queue, session_state, settings)
    )

    # Store on app.state for endpoint access
    app.state.settings = settings
    app.state.session = session_state
    app.state.queue = queue
    app.state.container_client = container_client
    app.state.ready = True

    yield

    # Shutdown
    watcher_task.cancel()
    for t in worker_tasks:
        t.cancel()
    gc_task.cancel()
    await asyncio.gather(watcher_task, *worker_tasks, gc_task, return_exceptions=True)
    await close_azure_client(blob_service_client, credential)
    tracer_provider.shutdown()
    meter_provider.shutdown()


app = FastAPI(lifespan=lifespan)
```

**Health endpoints**:

```python
@app.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse()


@app.get("/readyz")
async def readyz(request: Request) -> ReadyResponse:
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(status_code=503, detail="Not ready")
    return ReadyResponse()
```

Both are pure async. Zero NFS access. Zero `to_thread` calls.

**Watch start**:

```python
@app.post("/v1/watch/start")
async def watch_start(
    request: Request,
    body: WatchStartRequest,
) -> WatchStartResponse:
    session_state = request.app.state.session
    settings = request.app.state.settings

    if session_state.active:
        raise HTTPException(status_code=409, detail="Session already active")

    date_prefix, session_name = await start_session(
        session_state, settings, body.session_name,
    )
    return WatchStartResponse(
        date_prefix=date_prefix,
        session_name=session_name,
        encoded_session=session_name,
    )
```

**Watch stop**:

```python
@app.post("/v1/watch/stop")
async def watch_stop(request: Request) -> WatchStopResponse:
    stop_session(request.app.state.session)
    return WatchStopResponse(enabled=False)
```

**Status**:

```python
@app.get("/v1/status")
async def status(request: Request) -> StatusResponse:
    s = request.app.state.session
    return StatusResponse(
        enabled=s.active,
        active_session=s.session_name,
        processed_ok=s.processed_ok,
        processed_err=s.processed_err,
        last_error=s.last_error,
    )
```

**App entry point**:

```python
def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
```

---

## 12. Kubernetes Manifests

### `k8s/pv-nfs.yaml`

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: nfs-data-pv
spec:
  capacity:
    storage: 1Ti
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  mountOptions:
    - hard
    - nfsvers=4.1
    - actimeo=5
    - rsize=1048576
    - wsize=1048576
  nfs:
    server: "192.168.1.100"    # Replace with Jetson/NFS server IP
    path: /export/data
```

### `k8s/pvc-nfs.yaml`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nfs-data-pvc
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  resources:
    requests:
      storage: 1Ti
  volumeName: nfs-data-pv
```

### `k8s/deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nfs-watcher-uploader
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nfs-watcher-uploader
  template:
    metadata:
      labels:
        app: nfs-watcher-uploader
    spec:
      containers:
        - name: uploader
          image: nfs-watcher-uploader:latest
          ports:
            - containerPort: 8000
          env:
            - name: APP_AZURE_ACCOUNT_URL
              value: "https://<account>.blob.core.windows.net"
            - name: APP_AZURE_CONTAINER
              value: "ingest"
            - name: OTEL_SERVICE_NAME
              value: "nfs-watcher-uploader"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "http://otel-collector:4318"
          envFrom:
            - secretRef:
                name: azure-credentials
                optional: true
          volumeMounts:
            - name: nfs-data
              mountPath: /mnt/nfs
            - name: staging
              mountPath: /mnt/staging
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
              ephemeral-storage: 2Gi
            limits:
              cpu: "2"
              memory: 1Gi
              ephemeral-storage: 110Gi
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8000
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8000
          startupProbe:
            httpGet:
              path: /healthz
              port: 8000
            periodSeconds: 5
            failureThreshold: 24
      volumes:
        - name: nfs-data
          persistentVolumeClaim:
            claimName: nfs-data-pvc
        - name: staging
          emptyDir:
            sizeLimit: 100Gi
```

---

## Cross-Cutting Concerns

### Thread usage summary

| Operation | Runs in thread? | `abandon_on_cancel` |
|-----------|:-:|:-:|
| `os.scandir()` (NFS) | Yes | Yes |
| `os.rename()` (NFS) | Yes | Yes |
| `shutil.copy2()` (NFS→local) | Yes | Yes |
| `os.fsync()` (local) | Yes | Yes |
| `os.unlink()` (NFS) | Yes | Yes |
| `os.unlink()` (local) | No | N/A |
| `os.makedirs()` (NFS) | Yes | Yes |
| `os.makedirs()` (local) | No | N/A |
| `blob_client.upload_blob()` | No (native async) | N/A |

All NFS operations use `anyio.to_thread.run_sync(abandon_on_cancel=True)` so
the event loop is never blocked by NFS hangs. Local filesystem operations
are fast and don't need thread offloading.

### Logging conventions

All log calls include structured `extra` fields where applicable:

```python
logger.info(
    "Upload complete: %s",
    item.filename,
    extra={
        "file_name": item.filename,
        "session_name": item.session_name,
        "date_prefix": item.date_prefix,
        "blob_name": blob_name,
        "size_bytes": file_size,
        "duration_s": duration,
    },
)
```

### Error errno handling

```python
import errno

BENIGN_ERRNOS = {errno.ENOENT, errno.ESTALE}
```

Both `ENOENT` and `ESTALE` are treated identically throughout the codebase:
the file is gone (either deleted, renamed, or NFS handle is stale). In all
cases, the correct action is to skip the file.

---

## 13. Development Infrastructure

### Makefile

```makefile
.PHONY: install run dev-up dev-down docker-up docker-down docker-build \
        lint format typecheck clean k3s-apply k3s-delete

# --- Setup ---

install:
	pip install -e ".[dev]"

# --- Local development (app on host, deps in Docker) ---

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

dev-up:
	docker compose -f docker-compose.dev.yaml up -d

dev-down:
	docker compose -f docker-compose.dev.yaml down

# --- Docker Compose full stack ---

docker-build:
	docker build -t nfs-watcher-uploader:latest .

docker-up: docker-build
	docker compose up -d

docker-down:
	docker compose down

# --- Code quality ---

lint:
	ruff check app/

format:
	ruff format app/

typecheck:
	pyright app/

# --- k3s ---

k3s-apply:
	kubectl apply -f k8s/

k3s-delete:
	kubectl delete -f k8s/

# --- Cleanup ---

clean:
	rm -rf data/ __pycache__ .ruff_cache .pyright
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
```

### docker-compose.dev.yaml

Dependencies only — the app runs on the host. Used with `make dev-up`.

```yaml
services:
  azurite:
    image: mcr.microsoft.com/azure-storage/azurite
    ports:
      - "10000:10000"   # Blob
      - "10001:10001"   # Queue
      - "10002:10002"   # Table
    volumes:
      - azurite-data:/data
    command: azurite --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0

  otel-collector:
    image: otel/opentelemetry-collector-contrib
    ports:
      - "4317:4317"     # OTLP gRPC
      - "4318:4318"     # OTLP HTTP
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes:
      - ./otel-collector-config.yaml:/etc/otelcol/config.yaml:ro

volumes:
  azurite-data:
```

### docker-compose.yaml

Full stack — app, Azurite, OTel Collector all in containers. Used with
`make docker-up`.

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      APP_AZURE_ACCOUNT_URL: http://azurite:10000/devstoreaccount1
      APP_AZURE_CONTAINER: ingest
      APP_AZURE_CONNECTION_STRING: >-
        DefaultEndpointsProtocol=http;
        AccountName=devstoreaccount1;
        AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;
        BlobEndpoint=http://azurite:10000/devstoreaccount1;
      APP_NFS_INCOMING_DIR: /mnt/nfs/incoming
      APP_NFS_PROCESSING_ROOT: /mnt/nfs/.processing
      APP_LOCAL_STAGING_ROOT: /mnt/staging
      APP_POLL_INTERVAL_S: "1.0"
      APP_MIN_FILE_AGE_S: "2.0"
      APP_WORKER_CONCURRENCY: "2"
      OTEL_SERVICE_NAME: nfs-watcher-uploader
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
    volumes:
      - ./data/incoming:/mnt/nfs/incoming
      - ./data/.processing:/mnt/nfs/.processing
      - staging:/mnt/staging
    depends_on:
      - azurite
      - otel-collector

  azurite:
    image: mcr.microsoft.com/azure-storage/azurite
    ports:
      - "10000:10000"
      - "10001:10001"
      - "10002:10002"
    volumes:
      - azurite-data:/data
    command: azurite --blobHost 0.0.0.0 --queueHost 0.0.0.0 --tableHost 0.0.0.0

  otel-collector:
    image: otel/opentelemetry-collector-contrib
    ports:
      - "4317:4317"
      - "4318:4318"
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes:
      - ./otel-collector-config.yaml:/etc/otelcol/config.yaml:ro

volumes:
  azurite-data:
  staging:
```

### otel-collector-config.yaml

Minimal OTel Collector config that logs everything to the console. Replace
exporters with real backends (Jaeger, Prometheus, etc.) when needed.

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:

exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [debug]
```

### Azurite connection details

| Property | Value |
|----------|-------|
| Account name | `devstoreaccount1` |
| Account key | `Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==` |
| Blob endpoint (host) | `http://127.0.0.1:10000/devstoreaccount1` |
| Blob endpoint (Docker) | `http://azurite:10000/devstoreaccount1` |
| Connection string | See `.env.example` |

Azurite supports all Blob Storage APIs used by this service: `Put Blob`,
`Put Block`, `Put Block List`, `Get Container Properties`,
`Create Container`. The well-known account key is public and hardcoded
in Azurite — it is not a secret.

### Local development workflow

**First-time setup**:

```bash
# Clone and install
git clone <repo>
cd nfs-watcher-uploader
python -m venv .venv
source .venv/bin/activate
make install

# Copy env template
cp .env.example .env

# Create local data directories
mkdir -p data/incoming data/.processing data/staging
```

**Daily workflow**:

```bash
# Start Azurite
make dev-up

# Run the app (auto-reload on code changes)
make run

# In another terminal — start a session and drop test files
curl -X POST http://localhost:8000/v1/watch/start \
  -H 'Content-Type: application/json' \
  -d '{"session_name": "test-session"}'

# Copy test files into the watched directory
cp /path/to/testfile.bin data/incoming/test-session/

# Check status
curl http://localhost:8000/v1/status

# Verify upload in Azurite (using Azure CLI)
az storage blob list \
  --connection-string "$(grep APP_AZURE_CONNECTION_STRING .env | cut -d= -f2-)" \
  --container-name ingest \
  --output table

# Stop
curl -X POST http://localhost:8000/v1/watch/stop
make dev-down
```

**Full stack (Docker Compose)**:

```bash
# Start everything in containers
make docker-up

# The app is at http://localhost:8000
# Same curl commands as above

# View logs
docker compose logs -f app

# Stop
make docker-down
```

### NFS simulation notes

For local development, the NFS mount is replaced with local directories:

- `./data/incoming/` → `APP_NFS_INCOMING_DIR`
- `./data/.processing/` → `APP_NFS_PROCESSING_ROOT`
- `./data/staging/` → `APP_LOCAL_STAGING_ROOT`

`os.rename()` across these directories works identically to NFS (same
filesystem). The stability detection (`min_file_age_s`) still applies —
files must be older than the threshold and match in two consecutive scans.

In Docker Compose full-stack mode, NFS directories are bind-mounted from
the host's `./data/` into the container at `/mnt/nfs/`. The staging
volume uses a Docker named volume (ephemeral, matching production's
`emptyDir` behavior).
