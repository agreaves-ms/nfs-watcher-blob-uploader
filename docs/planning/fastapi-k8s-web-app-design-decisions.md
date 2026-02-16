# Design Decisions: NFS Watch FastAPI K8s Web App

> Consolidates user answers, research findings, and resolved design decisions.
>
> Guiding principles:
>
> - This service is a **simple shim** that moves files from NFS to Azure Blob Storage.
> - Use **modern language features and industry-standard frameworks** to minimize maintained code.
> - Avoid over-engineering. If a standard library feature or SDK default is sufficient, use it.
>
> Status: **All issues resolved** -- ready for implementation planning.

---

## Table of Contents

1. [Deployment Target](#1-deployment-target)
2. [Session Architecture](#2-session-architecture)
3. [NFS Configuration](#3-nfs-configuration)
4. [File Lifecycle](#4-file-lifecycle)
5. [Azure Blob Storage](#5-azure-blob-storage)
6. [Observability](#6-observability)
7. [Kubernetes / k3s](#7-kubernetes--k3s)
8. [Dependencies & Build](#8-dependencies--build)
9. [Failure & Recovery Model](#9-failure--recovery-model)
10. [PRD Deltas](#10-prd-deltas)

---

## 1. Deployment Target

**Decision**: Single-node k3s cluster on NVIDIA Jetson (ARM64, Ubuntu).

| Aspect | Detail |
|--------|--------|
| Hardware | NVIDIA Jetson (ARM64) |
| OS | Ubuntu (JetPack) |
| K8s distribution | k3s |
| Container runtime | containerd (k3s default) |
| Network | Secure local network, no internet-facing endpoints |
| Ephemeral storage | ~4TB available on node |
| NFS server | Same local network (possibly same Jetson) |

**Implications**:

- Docker images must target `linux/arm64`
- No need for auth, rate limiting, TLS, or mTLS
- Resource sizing must account for Jetson CPU/memory constraints
- k3s uses `/var/lib/rancher/k3s/agent/kubelet` (differs from standard K8s)
- k3s has built-in local-path provisioner; NFS uses native in-tree volumes

---

## 2. Session Architecture

### 2.1 Single-session per pod (preserved from PRD)

**Decision**: Each pod handles one active session at a time. `POST /v1/watch/start` while a session is active returns **409 Conflict**.

**Rationale**: Simplifies state management. Multiple different sessions are handled by running multiple pods (see 2.3).

### 2.2 Session auto-resume on startup

**Decision**: On startup, the service scans `.processing/` for existing session directories. If found, it re-enqueues all files and resumes the session without requiring a `POST /v1/watch/start` call.

**Design**:

- Scan `.processing/<date>/<session>/` directories
- Re-enqueue all files found (regardless of which session they belong to)
- Derive session name and date prefix from the directory path
- Start the watcher automatically for the corresponding NFS incoming subfolder
- If multiple session directories exist (edge case from crashes between sessions), re-enqueue files from all of them; the blob path is encoded in the directory structure, so uploads land in the right place regardless

### 2.3 Multi-session via multiple pods

**Decision**: Use a simple **Deployment** (not StatefulSet). Multiple replicas handle different sessions, with each session watching its own NFS subfolder (`/mnt/nfs/incoming/{{session}}/`).

**Rationale**: StatefulSet, Job/CronJob, and Operator patterns were evaluated. For a local Jetson deployment with manual session starts, a simple Deployment is the right level of complexity. Each pod receives its session assignment via the `POST /v1/watch/start` API call.

**Alternative considered**: StatefulSet with ordinal-based session assignment. Rejected because:

- Requires an assignment mechanism (who decides which pod gets which session?)
- Scaling up/down requires StatefulSet replica changes
- Overkill for manual, low-frequency session management

### 2.4 Session naming and validation

**Decision**: Auto-generated sessions use `00-session-<UUIDv7>` format. User-provided session names are validated against `[a-zA-Z0-9_\-.]` and rejected with 400 Bad Request if invalid.

**Package**: `uuid-utils` (Rust-backed via PyO3, fastest, RFC 9562 compliant).

**Rationale**:

- UUIDv7 embeds a millisecond timestamp, making it time-sortable lexicographically
- `uuid-utils` publishes pre-built `manylinux_aarch64` wheels (critical for Jetson)
- Forward-compatible with Python 3.14's built-in `uuid.uuid7()`
- API is compatible with `uuid.UUID` (works with Pydantic, SQLAlchemy, JSON serialization)
- Restrictive session name validation eliminates the need for URL encoding; the session name is used directly as an NFS subfolder name and blob path component

```python
from uuid_utils import uuid7

def generate_session_name() -> str:
    return f"00-session-{uuid7()}"
```

---

## 3. NFS Configuration

### 3.1 NFS server setup (on Jetson/Ubuntu)

**Decision**: Use `nfs-kernel-server` package with NFSv4.1.

```bash
# Package
sudo apt-get install nfs-kernel-server

# Export configuration (/etc/exports)
/export/data  192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)
```

**Key export options**:

- `rw` -- pods need to rename/delete files
- `sync` -- data integrity (write to disk before replying)
- `no_subtree_check` -- avoids stale handle issues
- `no_root_squash` -- pods running as root need access

### 3.2 NFS mount options (k3s PV)

**Decision**: Configure mount options in the PersistentVolume manifest.

```yaml
mountOptions:
  - hard
  - nfsvers=4.1
  - actimeo=5
  - rsize=1048576
  - wsize=1048576
```

**Rationale**:

- `hard` -- never give up on NFS operations (data integrity; silent failures are worse than hangs)
- `nfsvers=4.1` -- session semantics, exactly-once RPC semantics (EOS), better rename safety
- `actimeo=5` -- attribute cache timeout of 5 seconds; makes `min_file_age_s=5` sufficient for stability detection (since we control the NFS server, we can guarantee attribute freshness)
- `rsize/wsize=1048576` -- 1MB read/write sizes for large file performance

### 3.3 NFS directory structure (updated from PRD)

**Decision**: Sessions have subfolders under `incoming/`.

```
/mnt/nfs/
  incoming/
    {{session}}/              # Per-session subfolder
      file1.dat
      file2.bin
  .processing/
    YYYYMMDD/
      {{session}}/
        file1.dat
        file1.dat.completed   # After successful upload
```

**Change from PRD**: The PRD specified `/mnt/nfs/incoming/<filename>` flat structure and used URL-encoded session names. The new design uses `/mnt/nfs/incoming/{{session}}/` subfolders with validated (not encoded) session names, where `{{session}}` is specified in the `POST /v1/watch/start` request body.

### 3.4 NFS failure handling

**Decision**: Let the pod die and let Kubernetes restart it.

- NFS `hard` mount causes threads to enter D-state when NFS is unavailable
- Liveness probe (pure async, no NFS) times out after 30s
- Kubelet kills the pod; `restartPolicy: Always` restarts it
- When NFS comes back, the new pod resumes from `.processing`

No circuit breakers, health check threads, or NFS connectivity monitoring needed.

### 3.5 NFS error handling in code

- Catch `errno.ESTALE` alongside `errno.ENOENT` for stale file handles (both mean "file already gone, skip it")
- Use `os.scandir()` over `os.listdir()` (returns stat info with directory listing, reducing NFS RPCs)
- Use `anyio.to_thread.run_sync()` with `abandon_on_cancel=True` for NFS calls that could hang
- Use the default thread pool; with async Azure SDK, only NFS calls use threads (~5 concurrent at peak), well within the 40-thread default

---

## 4. File Lifecycle

### 4.1 File states (updated from PRD)

**Decision**: Four-state lifecycle with `.completed` marking.

```
incoming → .processing → .completed → deleted (by GC)
```

| State | Location | Description |
|-------|----------|-------------|
| `incoming` | `/mnt/nfs/incoming/{{session}}/file.dat` | New file from writer |
| `.processing` | `/mnt/nfs/.processing/YYYYMMDD/{{session}}/file.dat` | Claimed by worker |
| `.completed` | `/mnt/nfs/.processing/YYYYMMDD/{{session}}/file.dat.completed` | Upload succeeded |
| deleted | N/A | GC removed the file |

**Change from PRD**: The PRD specified "delete after upload." The new design renames to `.completed` after upload, and a background GC process handles deletion.

### 4.2 Work queue

**Decision**: Use `asyncio.Queue(maxsize=N)` as the bounded work queue between the watcher and workers.

**Rationale**: The PRD specified anyio memory channels, but `asyncio.Queue` is simpler for this use case:

- Built into the standard library (no clone lifecycle management)
- Multiple consumers call `queue.get()` natively
- `maxsize` provides backpressure (watcher blocks on `queue.put()` when full)
- Sufficient for a single-producer/multi-consumer pattern

### 4.3 File stability check

**Decision**: Two-consecutive-scan stability check with `(size, mtime)` matching + minimum age.

- `poll_interval_s = 2.0` (default)
- `min_file_age_s = 5.0` (default)
- File is "stable" when: `(size, mtime)` matches in two consecutive scans AND `mtime` is older than `min_file_age_s`
- With `actimeo=5` on the NFS mount, 5 seconds is sufficient for attribute freshness

### 4.4 File extension filtering

**Decision**: Configurable include-list of file extensions.

```
APP_FILE_EXTENSIONS=".bin,.mp4,.dat"
```

- If set, only files matching the listed extensions are considered for processing
- If empty/unset, all files are considered (backward-compatible with PRD)
- Filtering happens in the watcher's scan loop, before stability checking

### 4.5 Garbage collection process

**Decision**: Background GC task within the same service (not a sidecar or CronJob).

- Runs continuously as an asyncio task
- Scans `.processing/` for `.completed` files
- Deletes `.completed` files from NFS and corresponding local staging files
- Deletes immediately (no retention period)
- If NFS is unavailable, GC silently skips; pod will die from liveness probe shortly anyway
- GC cycle interval: `APP_GC_INTERVAL_S` (default: 30)

**Rationale**: Simplest approach. GC doesn't need its own pod lifecycle. If the service crashes, files stay in `.completed` and get cleaned up after restart.

---

## 5. Azure Blob Storage

### 5.1 SDK choice

**Decision**: Use the **async** Azure Storage SDK (`azure.storage.blob.aio`).

```python
from azure.storage.blob.aio import BlobServiceClient
from azure.identity.aio import DefaultAzureCredential
```

**Rationale**:

- Fits naturally with FastAPI's asyncio event loop
- `max_concurrency` uses asyncio tasks (not threads), avoiding thread pool consumption
- More modern; avoids `anyio.to_thread` wrappers for blob operations
- NFS I/O still uses `anyio.to_thread` (blocking I/O)

### 5.2 Client lifecycle

**Decision**: Use FastAPI `lifespan` context manager. Create client at startup, close at shutdown.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    credential = DefaultAzureCredential()
    blob_service_client = BlobServiceClient(
        account_url=settings.account_url,
        credential=credential,
    )
    # Fail-fast: validate auth and container
    container_client = blob_service_client.get_container_client(settings.container)
    try:
        await container_client.get_container_properties()
    except:
        try:
            await container_client.create_container()
        except Exception as e:
            await blob_service_client.close()
            await credential.close()
            raise SystemExit(f"Cannot access/create container: {e}")

    app.state.blob_service_client = blob_service_client
    app.state.container_client = container_client
    app.state.credential = credential

    yield

    await blob_service_client.close()
    await credential.close()
```

**Critical**: Must close BOTH the client AND the credential (credential has its own HTTP session).

### 5.3 Upload tuning

**Decision**: Use Azure SDK defaults. Expose as optional config env vars.

| Parameter | SDK Default | PRD Value | Decision |
|-----------|------------|-----------|----------|
| `max_single_put_size` | 64 MiB | 4 MiB | **SDK default (64 MiB)** |
| `max_block_size` | 4 MiB | 8 MiB | **SDK default (4 MiB)** |
| `max_concurrency` | 1 | 8 | **8** (keep PRD value) |

**Rationale**: All files are 100MB-10GB, so they will always use chunked upload regardless of `max_single_put_size`. The SDK defaults are well-tested. `max_concurrency=8` provides good parallelism without saturating the Jetson's uplink.

The tuning env vars (`APP_AZURE_MAX_BLOCK_SIZE`, `APP_AZURE_MAX_SINGLE_PUT_SIZE`, `APP_AZURE_MAX_CONCURRENCY`) are optional pass-through to the SDK, defaulting to SDK values.

### 5.4 Authentication

**Decision**: Keep `DefaultAzureCredential`. Accept the 30+ second startup delay on misconfiguration.

- Service runs on k3s on a secure local network
- The delay only happens once on startup if credentials are misconfigured (pod crashes)
- No need to optimize the credential chain
- For local development, set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` environment variables so `EnvironmentCredential` (position 1 in chain) picks them up immediately

### 5.5 Container creation

**Decision**: Attempt to create the container if it doesn't exist. Fail if creation also fails (missing permissions).

### 5.6 Idempotency

**Decision**: Last-writer-wins with `overwrite=True`. No ETags, no leases.

Each file maps to a unique blob name. Re-uploads produce identical content. This is safe and simple.

---

## 6. Observability

### 6.1 Telemetry stack

**Decision**: OpenTelemetry (traces, metrics, and logs) exported to OTel Collector.

**Packages**:

```
opentelemetry-api>=1.27.0
opentelemetry-sdk>=1.27.0
opentelemetry-exporter-otlp-proto-http>=1.27.0
opentelemetry-instrumentation-fastapi>=0.48b0
```

**Note**: Use HTTP exporter (`otlp-proto-http`) instead of gRPC (`otlp-proto-grpc`) to avoid `grpcio` compilation issues on ARM64. The HTTP exporter uses `requests` (pure Python) and is equally functional.

### 6.2 Auto-instrumentation

**Decision**: Use `FastAPIInstrumentor` for automatic HTTP span creation.

- Creates spans for each request with HTTP semantic convention attributes
- Exclude health check endpoints: `excluded_urls="healthz,readyz"`
- Propagates W3C `traceparent`/`tracestate` context

### 6.3 Custom metrics

| Metric | Type | Description |
|--------|------|-------------|
| `files.processed` | Counter | Successfully uploaded files |
| `files.failed` | Counter | Files that failed upload |
| `upload.duration` | Histogram | Upload time per file (seconds) |
| `file.size` | Histogram | Size of uploaded files (bytes) |
| `queue.depth` | UpDownCounter | Current work queue depth |

### 6.4 Structured logging

**Decision**: JSON structured logging with OTel trace context injection.

- Use Python's `logging` module with a custom JSON formatter
- Inject `trace_id`, `span_id`, `trace_flags` from current span context
- Include `file_name`, `session_name` as structured fields
- Quiet noisy loggers (`azure`, `uvicorn.access`)

### 6.5 OTel Collector deployment

**Decision**: DaemonSet on k3s, receiving OTLP on `localhost:4317` (gRPC) or `localhost:4318` (HTTP).

Application sends telemetry via OTLP HTTP to the collector. Collector configuration (exporters to final backends) is out of scope for this service's implementation.

Environment-based configuration:

```yaml
env:
  - name: OTEL_SERVICE_NAME
    value: "nfs-watcher-uploader"
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://otel-collector:4318"
```

---

## 7. Kubernetes / k3s

### 7.1 Deployment type

**Decision**: Standard `Deployment` with `replicas: 1` (default).

Multiple replicas can handle different sessions. Each replica gets its session via API call.

### 7.2 NFS PersistentVolume

**Decision**: Use native in-tree NFS volumes (no CSI driver needed).

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
    server: <jetson-ip>
    path: /export/data
```

**Prerequisite**: `nfs-common` must be installed on the k3s node (`sudo apt-get install nfs-common`).

### 7.3 emptyDir for local staging

**Decision**: `sizeLimit: 100Gi` on emptyDir. Set `ephemeral-storage` limits accordingly.

With 4TB node storage, 100Gi is conservative (~2.5% of capacity). Worst-case concurrent staging with 4 workers and 10GB max file size is 40GB.

### 7.4 Probes

| Probe | Endpoint | Config |
|-------|----------|--------|
| Liveness | `GET /healthz` | `periodSeconds: 10`, `failureThreshold: 3` (30s) |
| Readiness | `GET /readyz` | Standard defaults |
| Startup | `GET /healthz` | `failureThreshold: 24`, `periodSeconds: 5` (2 min) |

**Critical**: Liveness probe handler must be pure async -- no NFS access, no `to_thread`.

Startup probe accommodates `DefaultAzureCredential` chain exhaustion (30+ seconds on misconfiguration).

### 7.5 Shutdown behavior

**Decision**: Immediate death on SIGTERM. No graceful drain.

- Everything is idempotent; in-flight uploads are re-processed on restart
- No preStop hooks, no `--timeout-graceful-shutdown` tuning
- Default K8s behavior (SIGTERM -> 30s -> SIGKILL) is fine
- Files in `.processing` survive and get re-enqueued on restart

---

## 8. Dependencies & Build

### 8.1 Python dependencies

```toml
[project]
name = "nfs-watcher-uploader"
requires-python = ">=3.12"
dependencies = [
    # Web framework
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",

    # Azure
    "azure-storage-blob>=12.23.0",
    "azure-identity>=1.17.0",

    # UUIDv7
    "uuid-utils>=0.9.0",

    # OpenTelemetry - Core
    "opentelemetry-api>=1.27.0",
    "opentelemetry-sdk>=1.27.0",

    # OpenTelemetry - Exporter (HTTP to avoid grpcio on ARM64)
    "opentelemetry-exporter-otlp-proto-http>=1.27.0",

    # OpenTelemetry - FastAPI auto-instrumentation
    "opentelemetry-instrumentation-fastapi>=0.48b0",

    # Async I/O
    "anyio>=4.0.0",
]
```

### 8.2 Docker image

**Decision**: `python:3.12-slim-bookworm` base (multi-arch, supports `linux/arm64`).

- No NVIDIA L4T base needed (pure CPU/IO workload, no GPU)
- Multi-stage build to minimize final image size
- Build with `docker buildx` for multi-arch support

```bash
# For local Jetson build (ARM64 native):
docker build -t nfs-watcher-uploader:latest .

# For CI cross-compilation:
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag registry/nfs-watcher-uploader:latest \
  --push .
```

### 8.3 ARM64 compatibility notes

| Package | ARM64 Status |
|---------|-------------|
| `azure-storage-blob` | Pure Python + optional C extensions, works |
| `azure-identity` | Pure Python (`msal`), works |
| `uuid-utils` | Pre-built `manylinux_aarch64` wheels |
| `cryptography` | Rust-based; v42+ publishes ARM64 wheels |
| `grpcio` | v1.62+ publishes ARM64 wheels (but we avoid it with HTTP exporter) |
| `opentelemetry-*` | Pure Python, works |

---

## 9. Failure & Recovery Model

### 9.1 Philosophy

**Everything is idempotent. When in doubt, crash and restart.**

### 9.2 Failure scenarios

| Scenario | Behavior | Recovery |
|----------|----------|----------|
| NFS disconnects | Hard mount hangs threads; liveness probe fails; pod killed | Restart; auto-resume from `.processing` |
| Azure auth fails at startup | `SystemExit`; pod crashes | Fix credentials; pod restarts |
| Upload fails mid-way | Exception caught; file stays in `.processing` | Retry on next cycle or restart |
| Pod killed (SIGTERM/SIGKILL) | Immediate death | Restart; auto-resume from `.processing` |
| Duplicate file claim (race) | `ENOENT` caught; treated as benign | Skip file (already claimed by another worker/pod) |
| NFS stale handle | `ESTALE` caught; treated as `ENOENT` | Skip file |
| Disk pressure (emptyDir) | Kubelet eviction | Pod restarts; emptyDir cleared |

### 9.3 Idempotency guarantees

- **Blob upload**: `overwrite=True` + unique blob name = safe re-upload
- **File claim**: Atomic rename; second attempt gets `ENOENT` (benign)
- **Local staging**: `fsync` is best-effort, not required for correctness; if the local copy is lost, the file is still in `.processing` on NFS and gets re-copied on restart
- **Recovery**: `.processing` files re-enqueued; upload overwrites previous partial blob
- **GC**: Deleting `.completed` files is idempotent (already gone = no error)

---

## 10. PRD Deltas

Summary of decisions that change the PRD's original specification.

| PRD Section | Original | Decision | Rationale |
|-------------|----------|----------|-----------|
| FR-6: Cleanup | Delete after upload | Rename to `.completed` + GC | User preference (A7) |
| FR-7: Session | Single session, 409 on duplicate | Single session per pod; multi-pod for multi-session | User answer (A1, A11) |
| FR-7: Session naming | URL-encode session name | Validate `[a-zA-Z0-9_\-.]`, reject invalid; no encoding | Simplicity (FI-1) |
| FR-8: Recovery | Re-enqueue `.processing` files | Auto-resume active session + re-enqueue all | User answer (A2) |
| 8.1: NFS structure | Flat `/incoming/<filename>` | Per-session subfolder `/incoming/{{session}}/` | Multi-session architecture (A11) |
| 9: Concurrency | anyio memory channel work queue | `asyncio.Queue(maxsize=N)` | Simpler, stdlib, no clone lifecycle (AI-10) |
| 11: Upload tuning | Custom defaults (4MiB/8MiB) | SDK defaults (64MiB/4MiB) | User answer (A23); values were arbitrary |
| 14: Observability | Simple counters + structured-ish logs | Full OTel (traces, metrics, logs) | User answer (A16, A17) |
| NFR: Security | Consider auth/TLS | None needed | Secure local network (A19, A26) |
| NFR: Shutdown | Drain in-flight work | Immediate death | User answer (A18, A25) |
| New: File filtering | Not in PRD | Configurable extension include-list | User answer (A6) |
| New: GC process | Not in PRD | Background GC for `.completed` files | User answer (A7) |
| New: Auto-resume | Not in PRD | Auto-detect and resume session on startup | User answer (A2) |

---

*All issues resolved. Ready for implementation planning.*
*Last updated: 2026-02-15*
