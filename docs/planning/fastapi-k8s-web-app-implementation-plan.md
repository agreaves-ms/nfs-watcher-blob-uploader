# Implementation Plan: NFS Watch FastAPI K8s Web App

> Ordered implementation phases for the NFS-to-Azure-Blob upload service.
>
> Each phase produces a testable increment. Phases are sequential; modules
> within a phase may be implemented in parallel.
>
> For function signatures, data structures, and edge-case handling see
> [implementation details](./fastapi-k8s-web-app-implementation-details.md).

---

## Source Documents

| Document | Location |
|----------|----------|
| PRD | `docs/prds/fastapi-k8s-web-app.md` |
| Design decisions | `docs/planning/fastapi-k8s-web-app-design-decisions.md` |
| Resolved issues | `docs/planning/fastapi-k8s-web-app-issues.md` |
| Answered questions | `docs/planning/fastapi-k8s-web-app-questions.md` |
| Research | `.copilot-tracking/research/2026-02-15-fastapi-k8s-web-app-research.md` |

---

## Repository Structure

```
app/
  __init__.py
  main.py               # FastAPI app, lifespan, route wiring
  config.py              # Pydantic Settings from env vars
  models.py              # Request/response models, WorkItem
  session.py             # Session state: naming, validation, lifecycle
  watcher.py             # NFS polling loop (background asyncio task)
  worker.py              # Worker pool: claim → copy → upload → mark completed
  azure_client.py        # Async BlobServiceClient lifecycle, upload helper
  recovery.py            # Startup scan of .processing/, session auto-resume
  gc.py                  # Background GC for .completed files
  telemetry.py           # OTel setup: traces, metrics, structured logging
pyproject.toml
Dockerfile
k8s/
  deployment.yaml
  pv-nfs.yaml
  pvc-nfs.yaml
```

---

## Phase 1: Project Scaffolding

**Goal**: Buildable project with no runtime logic.

### Files

- [ ] `pyproject.toml` — project metadata, dependencies, entry point
- [ ] `app/__init__.py` — empty
- [ ] `Dockerfile` — multi-stage build targeting `linux/arm64`

### Details

**pyproject.toml** dependencies (from design decisions §8.1):

```
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
azure-storage-blob>=12.23.0
azure-identity>=1.17.0
uuid-utils>=0.9.0
opentelemetry-api>=1.27.0
opentelemetry-sdk>=1.27.0
opentelemetry-exporter-otlp-proto-http>=1.27.0
opentelemetry-instrumentation-fastapi>=0.48b0
anyio>=4.0.0
```

Python target: `>=3.12`.

**Dockerfile**: Two-stage build on `python:3.12-slim-bookworm`. First stage
installs dependencies; second stage copies the app. No NVIDIA base needed
(CPU/IO workload). See [details §1](./fastapi-k8s-web-app-implementation-details.md#1-project-scaffolding).

---

## Phase 2: Configuration

**Goal**: All settings loaded and validated at import time.

### Files

- [ ] `app/config.py`

### Design

Single `Settings` class using `pydantic-settings` (`BaseSettings`) with
`env_prefix="APP_"`. All env vars from the PRD plus design-decision additions.

| Group | Variables |
|-------|-----------|
| Azure | `AZURE_ACCOUNT_URL` (required), `AZURE_CONTAINER` (required), `AZURE_CONNECTION_STRING`, `AZURE_ACCOUNT_NAME`, `AZURE_ACCOUNT_KEY` |
| NFS | `NFS_INCOMING_DIR`, `NFS_PROCESSING_ROOT` |
| Staging | `LOCAL_STAGING_ROOT` |
| Watcher | `POLL_INTERVAL_S`, `MIN_FILE_AGE_S`, `FILE_EXTENSIONS` |
| Queue/Workers | `MAX_QUEUE_SIZE`, `WORKER_CONCURRENCY` |
| Azure upload | `AZURE_MAX_BLOCK_SIZE`, `AZURE_MAX_SINGLE_PUT_SIZE`, `AZURE_MAX_CONCURRENCY` |
| GC | `GC_INTERVAL_S` |

Defaults and validation rules: see [details §2](./fastapi-k8s-web-app-implementation-details.md#2-configuration).

---

## Phase 3: Models & Types

**Goal**: Shared data structures used across modules.

### Files

- [ ] `app/models.py`

### Design

- **Request/Response models** — Pydantic `BaseModel` subclasses for each endpoint
- **`WorkItem`** — `dataclass` representing a unit of work on the queue

`WorkItem` fields:

- `source_path: Path` — file location (incoming for new; .processing for recovery)
- `session_name: str`
- `date_prefix: str`
- `filename: str`
- `from_recovery: bool` — if `True`, skip claim step (file already in .processing)

See [details §3](./fastapi-k8s-web-app-implementation-details.md#3-models--types).

---

## Phase 4: Telemetry

**Goal**: OTel traces, metrics, and structured JSON logging configured before
any other subsystem starts.

### Files

- [ ] `app/telemetry.py`

### Design

- **TracerProvider** with OTLP HTTP exporter (avoids `grpcio` on ARM64)
- **MeterProvider** with OTLP HTTP exporter
- **LoggerProvider** with OTel log bridge
- **FastAPIInstrumentor** — auto-spans for HTTP; exclude `/healthz`, `/readyz`
- **JSON formatter** injecting `trace_id`, `span_id`, `trace_flags`
- **Custom metrics**: `files.processed` (Counter), `files.failed` (Counter),
  `upload.duration` (Histogram), `file.size` (Histogram), `queue.depth` (UpDownCounter)
- Quiet noisy loggers: `azure`, `uvicorn.access`

Telemetry is initialized first in the lifespan and shut down last.
Configuration via standard OTel env vars (`OTEL_SERVICE_NAME`,
`OTEL_EXPORTER_OTLP_ENDPOINT`).

See [details §4](./fastapi-k8s-web-app-implementation-details.md#4-telemetry).

---

## Phase 5: Azure Client

**Goal**: Authenticated async `BlobServiceClient` with fail-fast validation.

### Files

- [ ] `app/azure_client.py`

### Design

- Create `DefaultAzureCredential` (async variant from `azure.identity.aio`)
- Create `BlobServiceClient` (async from `azure.storage.blob.aio`)
- **Fail-fast**: call `get_container_properties()` on the target container.
  If it fails, attempt `create_container()`. If that also fails, `raise SystemExit`.
- **Fail-fast auth**: `DefaultAzureCredential()` does not fail at construction.
  Failure occurs on first use (the `get_container_properties()` call). If that
  raises `ClientAuthenticationError` and fallback creds are configured, close
  the credential, create a new client with fallback, and retry. If fallback
  also fails or no fallback is configured, `raise SystemExit`.
- `max_block_size` and `max_single_put_size` are set on the `BlobServiceClient`
  constructor (not on `upload_blob()`). `max_concurrency` is passed per-call.
- Expose an `upload_file()` coroutine that:
  1. Opens the local file (`open(path, "rb")`)
  2. Calls `blob_client.upload_blob(f, overwrite=True, blob_type="BlockBlob",
     max_concurrency=settings.azure_max_concurrency, length=file_size)`
- Client and credential stored on `app.state`; closed in lifespan teardown

**Critical**: close BOTH `BlobServiceClient` AND `DefaultAzureCredential`
on shutdown (credential owns its own HTTP session).

See [details §5](./fastapi-k8s-web-app-implementation-details.md#5-azure-client).

---

## Phase 6: Session Management

**Goal**: Session state machine with naming, validation, and directory creation.

### Files

- [ ] `app/session.py`

### Design

**State**: `SessionState` dataclass holding:

- `active: bool`
- `session_name: str | None`
- `date_prefix: str | None`
- `encoded_session: str | None` (same as `session_name` since no encoding needed)

**Validation**: session name must match `^[a-zA-Z0-9_.\-]+$`. Reject with
400 Bad Request if invalid.

**Auto-generation**: `00-session-<UUIDv7>` using `uuid_utils.uuid7()`.

**Directory creation on session start**:

- `/mnt/nfs/incoming/{{session}}/` (NFS incoming subfolder)
- `/mnt/nfs/.processing/YYYYMMDD/{{session}}/` (NFS processing)
- `/mnt/staging/YYYYMMDD/{{session}}/` (local staging)

Directories created with `os.makedirs(path, exist_ok=True)` via
`anyio.to_thread.run_sync()`.

**409 Conflict**: if a session is already active, `POST /v1/watch/start`
returns 409.

See [details §6](./fastapi-k8s-web-app-implementation-details.md#6-session-management).

---

## Phase 7: File Watcher

**Goal**: Background task that polls NFS and enqueues stable files.

### Files

- [ ] `app/watcher.py`

### Design

**Polling loop** (runs as an `asyncio.Task`):

1. Sleep `poll_interval_s`
2. If not enabled, continue sleeping
3. Call `os.scandir(incoming_dir)` via `anyio.to_thread.run_sync(abandon_on_cancel=True)`
4. For each entry:
   - Skip if not a file
   - Skip if extension not in allowed set (when `FILE_EXTENSIONS` is configured)
   - Skip if filename is in `pending` set (already enqueued, awaiting worker)
   - Record `(size, mtime)` from `DirEntry.stat()`
   - Compare against previous scan's record
   - If match AND `mtime` older than `min_file_age_s` → file is stable
5. Enqueue stable files as `WorkItem(from_recovery=False)` via `asyncio.Queue.put()`
6. Add enqueued filenames to `pending` set; increment `queue.depth` metric
7. Store current scan map for next iteration

**Pending set**: tracks filenames already enqueued but not yet claimed by a
worker. Prevents double-enqueue when the scan cycle is faster than the
worker's claim step. Entries are removed when the file disappears from the
incoming directory (indicating the worker renamed it) or when the file is
absent from two consecutive scans.

**Error handling**:

- `OSError` with `errno.ESTALE` or `errno.ENOENT`: log warning, skip entry
- Other `OSError`: log error, back off (exponential, capped at 60s), retry

**Backpressure**: `asyncio.Queue.put()` blocks when queue is full, naturally
throttling the watcher.

See [details §7](./fastapi-k8s-web-app-implementation-details.md#7-file-watcher).

---

## Phase 8: Worker Pool

**Goal**: N concurrent workers executing the per-file pipeline.

### Files

- [ ] `app/worker.py`

### Design

**Worker count**: `APP_WORKER_CONCURRENCY` (default 4). Each worker is an
`asyncio.Task` consuming from the shared `asyncio.Queue`.

**Per-file pipeline** (each step can fail independently):

1. **Claim** (skip if `from_recovery`):
   - `os.rename(incoming_path, processing_path)` via `anyio.to_thread.run_sync(abandon_on_cancel=True)`
   - Catch `ENOENT` / `ESTALE` → benign race, skip file

2. **Copy to local staging**:
   - `shutil.copy2(processing_path, staging_path)` via `anyio.to_thread.run_sync(abandon_on_cancel=True)`
   - `os.fsync()` on the destination (best-effort, not required for correctness)

3. **Upload to Azure**:
   - `await upload_file(container_client, staging_path, blob_name)`
   - `blob_name = f"{date_prefix}/{session_name}/{filename}"`
   - Records `upload.duration` and `file.size` metrics

4. **Mark completed**:
   - `os.rename(processing_path, processing_path + ".completed")` via thread
   - Delete local staging file

**Error handling per file**:

- Any exception → increment `files.failed`, log with file/session context,
  update `last_error`. File stays in `.processing` for retry on next
  restart/recovery.
- Success → increment `files.processed`, decrement `queue.depth`.

See [details §8](./fastapi-k8s-web-app-implementation-details.md#8-worker-pool).

---

## Phase 9: Garbage Collection

**Goal**: Background task cleaning up `.completed` files.

### Files

- [ ] `app/gc.py`

### Design

**GC loop** (runs as an `asyncio.Task`):

1. Sleep `gc_interval_s` (default 30)
2. Walk `/mnt/nfs/.processing/` via `os.walk()` in a thread
3. For each file ending in `.completed`:
   - Delete from NFS: `os.unlink(path)` via thread
   - Delete corresponding local staging file if it exists
4. Remove empty date/session directories

**Error handling**: catch all `OSError`, log warning, continue. If NFS is
unavailable, GC silently skips; the pod will die from liveness probe failure.

See [details §9](./fastapi-k8s-web-app-implementation-details.md#9-garbage-collection).

---

## Phase 10: Recovery

**Goal**: On startup, scan `.processing/` and re-enqueue unfinished files;
auto-resume the active session.

### Files

- [ ] `app/recovery.py`

### Design

**Startup recovery** (called during lifespan, before starting watcher):

1. Walk `/mnt/nfs/.processing/` for all `YYYYMMDD/{{session}}/` directories
2. Skip files ending in `.completed`
3. For each non-completed file, create `WorkItem(from_recovery=True)` with
   `date_prefix` and `session_name` parsed from the directory path
4. Enqueue all items
5. If any session directories found, set the active session to the
   lexicographically last `(date_prefix, session_name)` tuple (most recent)
6. Start watcher for the recovered session's incoming subfolder

**Edge case**: multiple session directories from crashes between sessions.
All files are re-enqueued (blob paths are encoded in directory structure).
Watcher resumes for the most recent session only.

See [details §10](./fastapi-k8s-web-app-implementation-details.md#10-recovery).

---

## Phase 11: API Endpoints & Main App

**Goal**: Wire all modules together via FastAPI lifespan; expose HTTP
endpoints.

### Files

- [ ] `app/main.py`

### Design

**Lifespan** (ordered):

1. Load `Settings`
2. Initialize telemetry (TracerProvider, MeterProvider, LoggerProvider)
3. Initialize Azure client (fail-fast auth + container validation)
4. Initialize session state
5. Create `asyncio.Queue(maxsize=settings.max_queue_size)`
6. Run recovery (scan .processing, auto-resume session)
7. Start GC task
8. Start worker tasks (N workers)
9. Start watcher task (if session was auto-resumed)
10. `yield` (app is serving)
11. Cancel watcher, workers, GC tasks
12. Close Azure client + credential
13. Shut down telemetry providers

**Endpoints**:

| Method | Path | Handler |
|--------|------|---------|
| `GET` | `/healthz` | Pure async, returns `{"ok": true}` |
| `GET` | `/readyz` | Returns `{"ready": true}` after lifespan completes |
| `POST` | `/v1/watch/start` | Start session + watcher |
| `POST` | `/v1/watch/stop` | Stop watcher, leave workers to drain |
| `GET` | `/v1/status` | Return session state + counters |

**`POST /v1/watch/stop` semantics**: stops the watcher loop (no new files
enqueued). Workers continue processing items already dequeued. Queued items
not yet dequeued remain in the queue; since workers are still running, the
queue drains naturally. Files in `.processing` that don't finish before
the next restart get re-enqueued via recovery.

**`/healthz` and `/readyz`**: must be pure async with zero NFS access. No
`anyio.to_thread`. If NFS hangs, these still respond, allowing kubelet to
detect the D-state via liveness probe timeout.

See [details §11](./fastapi-k8s-web-app-implementation-details.md#11-api-endpoints--main-app).

---

## Phase 12: Kubernetes Manifests

**Goal**: Deployable k3s manifests.

### Files

- [ ] `k8s/pv-nfs.yaml`
- [ ] `k8s/pvc-nfs.yaml`
- [ ] `k8s/deployment.yaml`

### Design

**PersistentVolume** (NFS):

- `nfs.server`: placeholder IP
- `nfs.path`: `/export/data`
- `mountOptions`: `hard`, `nfsvers=4.1`, `actimeo=5`, `rsize=1048576`, `wsize=1048576`
- `accessModes`: `ReadWriteMany`
- `persistentVolumeReclaimPolicy`: `Retain`

**PersistentVolumeClaim**: binds to the NFS PV.

**Deployment**:

- `replicas: 1`
- Volumes: NFS PVC at `/mnt/nfs`, emptyDir at `/mnt/staging` (`sizeLimit: 100Gi`)
- Probes:
  - Liveness: `GET /healthz`, `periodSeconds: 10`, `failureThreshold: 3`
  - Readiness: `GET /readyz`
  - Startup: `GET /healthz`, `periodSeconds: 5`, `failureThreshold: 24`
- Env vars: all `APP_*` from config, plus `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`
- No preStop hook (immediate death)
- Image: `nfs-watcher-uploader:latest`

See [details §12](./fastapi-k8s-web-app-implementation-details.md#12-kubernetes-manifests).

---

## Resolved Design Gaps

Issues identified during plan creation that were not fully addressed in prior
planning documents.

### Gap 1: `POST /v1/watch/stop` semantics

**PRD**: "workers may continue to process items already queued."
**FI-7**: "Stop immediately. Queued items stay in `.processing`."

**Resolution**: stop the watcher loop only. Workers continue processing their
current in-flight items and any remaining items in the queue. The queue drains
naturally. This matches the PRD behavior and is simpler than canceling workers
(which would require re-enqueueing partially processed items).

### Gap 2: Recovery with multiple session directories

**Resolution**: on recovery, re-enqueue files from ALL session directories
found in `.processing/`. Set the active session to the lexicographically last
`(date_prefix, session_name)` tuple (most recent). Start the watcher for that
session's incoming subfolder. The user can call `POST /v1/watch/stop` then
`POST /v1/watch/start` to switch to a different session.

### Gap 3: WorkItem structure

**Resolution**: defined as a `dataclass` with `source_path`, `session_name`,
`date_prefix`, `filename`, and `from_recovery` fields. See Phase 3.

### Gap 4: Directory creation responsibilities

**Resolution**: session start creates all three directory trees (NFS incoming
subfolder, NFS processing, local staging). Workers create subdirectories only
if they don't exist (defensive `makedirs` with `exist_ok=True`). Recovery
does not create directories (they already exist from the original session).

### Gap 5: Upload file I/O pattern

**Resolution**: the async Azure SDK's `upload_blob()` accepts sync file-like
objects (`open(path, "rb")`). The SDK handles chunked reading internally. Since
the file is on local ephemeral storage (not NFS), reads are fast and do not
block the event loop. The `length` parameter is passed to avoid the SDK
calling `seek()`/`tell()` to determine file size.

### Gap 6: Double-enqueue in watcher

Without mitigation, a file that passes the stability check can be enqueued
again on the next scan cycle if the worker hasn't claimed it yet.

**Resolution**: the watcher maintains a `pending: set[str]` of filenames
already enqueued. Files in `pending` are skipped during stability evaluation.
Entries are pruned each cycle when the file disappears from the incoming
directory (indicating the worker renamed it to `.processing`).

### Gap 7: `max_block_size` / `max_single_put_size` placement

These parameters are set on the `BlobServiceClient` constructor, not on
`upload_blob()`. Only `max_concurrency` is a per-call parameter.

**Resolution**: pass `max_block_size` and `max_single_put_size` from config
to the `BlobServiceClient` constructor during client creation. The
`upload_file()` helper only accepts `max_concurrency`.

### Gap 8: Azure fallback auth detection

`DefaultAzureCredential()` does not fail at construction — failure occurs on
first use (the container validation call).

**Resolution**: wrap `get_container_properties()` in a try/except for
`ClientAuthenticationError`. If caught and fallback credentials are configured,
close the original client/credential and create a new client with the fallback.
If no fallback exists or the fallback also fails, `raise SystemExit`.

---

## Dependency Graph

```
Phase 1: Scaffolding
  │
  ▼
Phase 2: Configuration ──────────────────────┐
  │                                           │
  ▼                                           │
Phase 3: Models ──────────┐                   │
  │                       │                   │
  ▼                       ▼                   ▼
Phase 4: Telemetry    Phase 5: Azure    Phase 6: Session
  │                       │                   │
  └───────────┬───────────┘                   │
              │                               │
              ▼                               │
         Phase 7: Watcher ◄───────────────────┤
              │                               │
              ▼                               │
         Phase 8: Workers ◄───────────────────┘
              │
              ▼
         Phase 9: GC
              │
              ▼
         Phase 10: Recovery
              │
              ▼
         Phase 11: Main App (wires everything)
              │
              ▼
         Phase 12: K8s Manifests
```

Phases 4, 5, and 6 can be implemented in parallel once Phases 2–3 are done.

---

## PRD Acceptance Criteria Mapping

| Criterion | Phase |
|-----------|-------|
| Session with no name creates `YYYYMMDD/00-session-<UUIDv7>/` blob folder | 6, 8 |
| Session with name creates `YYYYMMDD/<name>/` blob folder | 6, 8 |
| File moved to `.processing`, copied to staging, uploaded as Block Blob, deleted from NFS only after upload | 7, 8, 9 |
| Pod restart recovers from `.processing` and overwrites blob | 10 |
| FastAPI remains responsive during processing | 7, 8, 11 |
| Azure auth misconfiguration crashes pod with clear logs | 5, 11 |
