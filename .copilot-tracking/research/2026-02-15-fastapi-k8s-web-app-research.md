# Research Findings: NFS Watch FastAPI K8s Web App

> Date: 2026-02-15
> PRD: docs/prds/fastapi-k8s-web-app.md

---

## 1. NFS Atomic Rename Behavior

### 1.1 POSIX rename(2) Guarantees

- On a **local filesystem**, `rename()` is atomic with respect to other filesystem operations. If `newpath` already exists, it is atomically replaced.
- **Cross-device renames** return `EXDEV` (errno 18). `shutil.move()` falls back to copy+delete when crossing mount boundaries.
- **Source**: `man 2 rename` (man7.org/linux/man-pages/man2/rename.2.html)

### 1.2 Python's os.rename() vs os.replace()

- `os.rename(src, dst)` maps directly to the C `rename()` syscall on Unix/Linux.
- `os.replace(src, dst)` (Python 3.3+) is identical to `os.rename()` on Unix. On Windows, it adds atomic replacement of existing files.
- For Linux containers, `os.rename()` and `os.replace()` are equivalent.
- Neither provides cross-filesystem moves.

### 1.3 NFSv3 Rename Semantics (RFC 1813)

The NFSv3 RENAME operation (procedure 14):

- **Server-side atomicity**: RFC 1813, Section 3.3.14 states: *"The operation is required to be atomic to the client."* If the target exists, replacement is atomic from the client's perspective.
- **Weaker than POSIX atomic**: NFS protocol v3 is stateless. The server performs the rename atomically, but if the RPC times out, the client doesn't know whether the rename completed. The client will retry (with `hard` mount), and the second attempt fails with `ENOENT` (source gone). This is handled silently by the NFS client layer.
- **"Silly rename" problem**: When a file is open by a process and another process renames it, the Linux NFS client performs a "silly rename" to `.nfs<hex><hex>`. Not relevant to this use case (files are not held open during rename).

**Key for our use case**: A rename from `/mnt/nfs/incoming/file` to `/mnt/nfs/.processing/date/session/file` stays on the same NFS mount. The NFS client sends a single RENAME RPC. The server performs this atomically. **This is safe for claiming files.**

### 1.4 NFSv4 Improvements (RFC 7530, RFC 5661)

- **Stateful protocol**: NFSv4 has session semantics, reducing ambiguity around retried operations.
- **Exactly-once semantics (NFSv4.1+)**: RFC 5661 introduces sessions with EOS via slot tables and sequence IDs. Retried RENAME RPCs return cached results rather than re-executing.
- **Recommendation**: Use NFSv4.1+ if possible.

### 1.5 Known Race Conditions

| Race | Description | Mitigation |
|------|-------------|------------|
| Two clients rename same file | Second gets `ENOENT` | Handle as benign (PRD already specifies this) |
| Client attribute caching | Stale directory listings | Lower `actimeo` values or `noac` |
| Target directory doesn't exist | `ENOENT` or `ENOTDIR` | Always `os.makedirs(target_dir, exist_ok=True)` before rename |
| Partially written source file | Renamed while writer still active | Stability check (two consecutive scans + min age) |

### 1.6 Rename Atomicity Summary

| Property | Local ext4/xfs | NFSv3 | NFSv4.0 | NFSv4.1+ |
|----------|---------------|-------|---------|----------|
| Server-side atomic | Yes | Yes | Yes | Yes |
| Client sees atomic result | Yes | Yes (if RPC completes) | Yes | Yes |
| Safe on RPC retry | N/A | No (ENOENT, handled) | Better | Yes (EOS) |
| Same-mount required | Yes | Yes | Yes | Yes |

---

## 2. NFS File Watching / Polling Best Practices

### 2.1 Why inotify Does Not Work on NFS

- `inotify` operates at the Linux VFS layer, registering watches on **local kernel inodes**.
- On NFS, modifications happen on the **NFS server's kernel**. NFS has **no push notification mechanism**.
- inotify watches on NFS directories will only fire for changes made by processes on the **same client machine**.
- The inotify(7) man page explicitly states: *"The inotify API does not provide notification for files that are accessed via NFS mounts."*
- NFSv4 delegations are a caching optimization, not a notification mechanism.

### 2.2 Recommended Polling Strategy

```
Every poll_interval_s seconds:
  1. List incoming directory (os.scandir)
  2. For each entry:
     a. stat() to get size and mtime
     b. Check if (size, mtime) matches PREVIOUS scan's record
     c. Check if mtime is older than min_file_age_s
     d. If both conditions met -> file is "stable" -> enqueue
  3. Store current scan's {filename: (size, mtime)} for next comparison
```

### 2.3 Pitfalls of stat() on NFS

**Attribute caching**: NFS client caches file attributes per mount options:

- `acregmin` (default 3s), `acregmax` (default 60s) for files
- `acdirmin` (default 30s), `acdirmax` (default 60s) for directories
- **`min_file_age_s` should be >= `acregmax`** to guarantee attributes are fresh

**stat() can hang**: With `hard` mounts, `stat()` blocks indefinitely if NFS server is unreachable. Must run in background threads with application-level timeouts.

**Stale file handles (ESTALE)**: If a file is deleted/renamed on the server while client has cached entry. Catch `OSError` with `errno.ESTALE` and treat as `ENOENT`.

**os.scandir() on NFS**: Preferred over `os.listdir()`. With `readdirplus` (NFSv3) or READDIR with attributes (NFSv4), stat info comes back with directory listing, reducing RPCs.

### 2.4 Polling Interval Guidance

| File volume | Recommended poll_interval_s |
|-------------|----------------------------|
| Low (< 10/min) | 5-10s |
| Medium (10-100/min) | 2-5s |
| High (100+/min) | 1-2s |

PRD default of 2.0s is reasonable for medium-to-high throughput.

---

## 3. NFS Mount Options for Kubernetes

### 3.1 Hard vs Soft Mounts

**Hard mount (default, recommended)**:

- All NFS operations block indefinitely until server comes back
- Process enters uninterruptible sleep (D state) -- cannot be killed even with SIGKILL
- Guarantees data integrity: no operation silently fails

**Soft mount (NOT recommended)**:

- Operations return `EIO` after retries exhausted
- Linux NFS maintainers warn: *"Using the soft option is not recommended. It can cause silent data corruption."*
- Risk: a write/rename that fails transiently returns EIO, and the application may not handle correctly

### 3.2 Key Mount Options

| Option | Default | Recommendation |
|--------|---------|---------------|
| `hard` | Yes | Keep (data integrity) |
| `actimeo=N` | varies | Set to 3-10 for faster file detection |
| `acregmax=N` | 60s | Lower to 5-15s for polling use case |
| `acdirmin=N` | 30s | Lower to 1-5s |
| `lookupcache=none` | `all` | Useful for frequently appearing/disappearing files |
| `nfsvers=4.1` | Negotiated | Prefer 4.1+ for session semantics |
| `rsize=N` / `wsize=N` | Negotiated | Default (usually 1MB) is fine |
| `nordirplus` | No | Do NOT set -- READDIRPLUS reduces stat() RPCs |

### 3.3 Kubernetes PV Mount Options

```yaml
apiVersion: v1
kind: PersistentVolume
spec:
  nfs:
    server: nfs-server.example.com
    path: /export/incoming
  mountOptions:
    - hard
    - nfsvers=4.1
    - actimeo=5
    - rsize=1048576
    - wsize=1048576
```

### 3.4 NFS Hangs and Kubernetes Liveness Probes

**The problem**: NFS server unreachable -> threads enter D state -> if all threads stuck, HTTP server cannot accept connections -> liveness probe times out -> pod killed -> new pod mounts same NFS -> same hang -> CrashLoopBackOff.

**Critical mitigations**:

1. **Liveness probe must be pure async** -- no NFS, no thread dispatch:

   ```python
   @app.get("/healthz")
   async def healthz():
       return {"ok": True}  # MUST NOT touch NFS or use to_thread
   ```

2. **Generous probe thresholds**: `failureThreshold: 6` with `periodSeconds: 10` = 60s tolerance

3. **Use startup probe** for slow NFS mounts: `failureThreshold: 24` with `periodSeconds: 5` = 2min startup

4. **Thread pool sizing**: Ensure pool is large enough that NFS-blocked threads don't exhaust it (default anyio pool = 40)

5. **Consider dedicated NFS health thread** that records connectivity status for readiness probe

### 3.5 Azure-Specific NFS Considerations

- **Azure NetApp Files**: Supports NFSv3 and NFSv4.1. Prefer NFSv4.1.
- **Azure Blob Storage NFS v3**: Only NFSv3, no file locking, limited support.
- **Azure Files NFS**: NFSv4.1 only, POSIX-like semantics.

---

## 4. Azure Blob Storage Block Blob Parallel Upload

### 4.1 SDK Package

- `azure-storage-blob` (12.x series)
- Install: `pip install azure-storage-blob azure-identity`

### 4.2 Upload Decision Tree

**Tier 1 -- Single PUT vs Chunked:**

- Blob size <= `max_single_put_size` (default **64 MiB**): single `Put Blob` REST call
- Blob size > threshold OR size unknown: chunked upload

**Tier 2 -- Parallel Block Upload:**

1. Read blocks of `max_block_size` from data source
2. Assign unique block IDs
3. Issue `Put Block` (stage_block) REST calls, up to `max_concurrency` in parallel
4. After all blocks staged, single `Put Block List` atomically commits

### 4.3 Key Parameters and Defaults

| Parameter | Set On | Default | Description |
|-----------|--------|---------|-------------|
| `max_single_put_size` | Client constructor | **64 MiB** | Threshold for single-PUT vs chunked |
| `max_block_size` | Client constructor | **4 MiB** | Chunk size for block uploads |
| `max_concurrency` | `upload_blob()` | **1** | Parallel upload threads |

**PRD discrepancy noted**: The PRD specifies `APP_AZURE_MAX_SINGLE_PUT_SIZE` default as 4MiB, but the SDK default is 64MiB. The PRD also specifies `APP_AZURE_MAX_BLOCK_SIZE` default as 8MiB vs SDK's 4MiB. These choices may be intentional for the NFS use case.

### 4.4 Block Blob Limits

- Maximum 50,000 committed blocks per blob
- Maximum block size: 4,000 MiB (service version 2019-12-12+)
- Maximum blob size via Put Block List: ~190.7 TiB

### 4.5 Async SDK Support

```python
from azure.storage.blob.aio import BlobServiceClient
from azure.identity.aio import DefaultAzureCredential
```

The async path uses asyncio rather than thread pools for concurrency. This is relevant since the PRD uses FastAPI (asyncio-based).

---

## 5. DefaultAzureCredential Behavior

### 5.1 Credential Chain (in order)

| Order | Credential | Enabled by Default |
|-------|-----------|-------------------|
| 1 | EnvironmentCredential | Yes |
| 2 | WorkloadIdentityCredential | Yes |
| 3 | ManagedIdentityCredential | Yes |
| 4 | SharedTokenCacheCredential | Yes |
| 5 | VisualStudioCodeCredential | No |
| 6 | AzureCliCredential | Yes |
| 7 | AzurePowerShellCredential | Yes |
| 8 | AzureDeveloperCliCredential | Yes |
| 9 | InteractiveBrowserCredential | No |

### 5.2 Workload Identity on AKS

The AKS workload identity webhook injects into pods labeled with `azure.workload.identity/use: "true"`:

- `AZURE_CLIENT_ID` -- managed identity or app registration client ID
- `AZURE_TENANT_ID` -- Azure tenant ID
- `AZURE_FEDERATED_TOKEN_FILE` -- path to projected service account token
- `AZURE_AUTHORITY_HOST` -- Microsoft Entra authority URL

WorkloadIdentityCredential (position 2) exchanges the K8s service account token for a Microsoft Entra access token via OIDC federation.

### 5.3 Common Failure Modes

| Failure Mode | Impact |
|-------------|--------|
| Full chain exhaustion (no creds) | **30+ seconds** due to IMDS timeout + subprocess timeouts |
| Wrong managed identity client ID | Auth error, chain continues to next credential |
| Missing pod label for workload identity | Webhook doesn't inject env vars, falls through chain |
| Federated identity not configured | Token exchange fails |

**Production recommendation**: Replace `DefaultAzureCredential` with `ChainedTokenCredential` containing only the expected credential types (e.g., `WorkloadIdentityCredential` + fallback). Or use `AZURE_TOKEN_CREDENTIALS=prod` (azure-identity 1.23.0+) to skip developer credentials.

### 5.4 Debugging

```python
import logging
logging.getLogger("azure.identity").setLevel(logging.DEBUG)
```

---

## 6. Azure Blob Upload Idempotency and Overwrite

### 6.1 Overwrite with `overwrite=True`

**Small blobs (single Put Blob)**:

- Atomic: entire blob replaced or operation fails
- All existing metadata overwritten

**Large blobs (Put Block + Put Block List)**:

- `Put Block` stages blocks in **uncommitted state** (invisible to readers)
- Existing committed blob remains fully readable during staging
- `Put Block List` is the **atomic commit point** -- atomically replaces content

### 6.2 Partial Upload Failure

If upload fails mid-way (some Put Blocks succeed, Put Block List never called):

- Uncommitted blocks remain in uncommitted block list
- **Original committed blob is untouched** -- readers see previous version
- Uncommitted blocks garbage collected after **one week** of inactivity
- New `Put Blob` also triggers garbage collection of uncommitted blocks

**Uploads are transactional at the Put Block List level.**

### 6.3 Concurrent Uploads (Two Clients, Same Blob)

Without conditional headers, **last writer wins**:

- Both clients succeed (no error)
- Final blob content is from whichever `Put Block List` commits last
- Content is always from one complete upload, never mixed
- Azure docs warn: *"If your app requires multiple processes writing to the same blob, you should implement a strategy for concurrency control."*

### 6.4 Concurrency Control Options

| Strategy | Mechanism | Use Case |
|----------|-----------|----------|
| Optimistic (ETags) | `If-Match` conditional header | Detect concurrent modifications |
| Pessimistic (Leases) | 15-60s or infinite blob lease | Exclusive write access |
| Last Writer Wins | No conditions (default) | Idempotent overwrites |

For this service, **last-writer-wins with `overwrite=True` is appropriate** since each file maps to a unique blob name and re-uploads produce identical content.

---

## 7. FastAPI Background Tasks with anyio.to_thread

### 7.1 anyio.to_thread.run_sync

- Offloads blocking synchronous I/O to a worker thread from async context
- **Default thread pool: 40 threads** (via `CapacityLimiter`)
- When all tokens consumed, subsequent `run_sync` calls **await** (don't fail) until a token is released

### 7.2 Configuring Thread Pool

**Adjust default limiter**:

```python
from anyio import to_thread
limiter = to_thread.current_default_thread_limiter()
limiter.total_tokens = 100
```

**Custom limiter per operation**:

```python
upload_limiter = CapacityLimiter(10)
await to_thread.run_sync(blocking_func, limiter=upload_limiter)
```

### 7.3 Key Pitfalls

1. **`abandon_on_cancel=False` (default)**: Cancellation waits for thread to finish. For NFS I/O that could hang, consider `abandon_on_cancel=True`.
2. **Thread pool exhaustion**: All 40 tokens consumed -> cascading latency. Size appropriately.
3. **No async from thread**: Cannot call async code from within `run_sync`. Use `anyio.from_thread.run()`.
4. **GIL**: Doesn't help with CPU-bound work.
5. **FastAPI sync endpoints**: Already dispatched via `run_in_threadpool` (wraps `anyio.to_thread.run_sync`).

---

## 8. anyio Memory Channels for Work Queues

### 8.1 API

```python
send_stream, receive_stream = create_memory_object_stream[WorkItem](max_buffer_size=100)
```

### 8.2 Buffer Size Behavior

| Value | Behavior |
|-------|----------|
| `0` (default) | Unbuffered/rendezvous. `send()` blocks until receiver ready. |
| `N > 0` | Buffered. `send()` blocks when buffer full. |
| `math.inf` | Unbounded. `send()` never blocks (OOM risk). |

### 8.3 Backpressure

- Buffer full: `send()` suspends sending task until space available
- Buffer empty: `receive()` suspends receiving task until item available
- `send_nowait()` / `receive_nowait()` raise `WouldBlock` instead of suspending

### 8.4 Close Semantics

- Closing send stream: pending `receive()` gets remaining items, then `EndOfStream`
- Closing receive stream: pending `send()` gets `ClosedResourceError`
- **Clone support**: `.clone()` creates another reference. Channel only "closed" when all clones closed.
- **Critical**: if you clone for N workers, you must close the original or the channel never ends.

---

## 9. Kubernetes Graceful Shutdown with FastAPI

### 9.1 Shutdown Sequence

```
1. Pod marked "Terminating"
2. Pod removed from Service endpoints (async, may take seconds)
3. preStop hook runs (if configured)
4. SIGTERM sent to PID 1
5. terminationGracePeriodSeconds countdown (default: 30s)
6. SIGKILL if still alive
```

**Steps 2 and 3/4 happen concurrently** -- pod may receive traffic briefly after SIGTERM.

### 9.2 Uvicorn SIGTERM Handling

1. Sets `should_exit` flag
2. Stops accepting new connections
3. Sends ASGI `lifespan.shutdown` event
4. Waits for in-flight requests (`--timeout-graceful-shutdown` setting, uvicorn >= 0.24.0)

### 9.3 Best Practices

1. **preStop sleep** (5s) for endpoint propagation race
2. **`terminationGracePeriodSeconds`** > preStop + drain time
3. **Lifespan shutdown** to drain work queues
4. **`--timeout-graceful-shutdown`** < terminationGracePeriodSeconds - preStop

---

## 10. emptyDir Volume Sizing

### 10.1 Configuration

```yaml
volumes:
  - name: staging
    emptyDir:
      medium: ""        # disk-backed (default) or "Memory" (tmpfs)
      sizeLimit: 500Mi  # optional limit
```

### 10.2 What Happens When Limit Exceeded

- **Not an immediate filesystem error** -- the write succeeds
- Kubelet eviction manager detects overage asynchronously (~10s interval)
- Pod is **evicted** (status: Failed, reason: Evicted)
- Cannot rely on `sizeLimit` for hard filesystem-level blocking

### 10.3 Ephemeral Storage Resources

```yaml
resources:
  requests:
    ephemeral-storage: "2Gi"    # scheduling hint
  limits:
    ephemeral-storage: "4Gi"    # hard limit triggers eviction
```

Ephemeral storage counts: container writable layer + log files + disk-backed emptyDir volumes.

### 10.4 Key Implication for PRD

With `APP_WORKER_CONCURRENCY=4` and 100MB+ files, worst case = 4 files * 100MB+ = 400MB+ in staging at once. With queue of 2000 items, **the emptyDir `sizeLimit` is the real bottleneck**, not the queue size. Must size emptyDir for concurrent worker count, not queue depth.

---

## 11. UUIDv7 in Python (Time-Sortable UUIDs)

### 11.1 Python stdlib Support

**Python 3.14** (releasing October 2026) adds native `uuid.uuid7()` conforming to RFC 9562:
- `uuid.uuid7()` -- 48-bit Unix millisecond timestamp + 74-bit random (with monotonic counter)
- `uuid.uuid6()` -- reordered UUIDv1 fields for DB locality
- `uuid.uuid8()` -- custom blocks for application-defined semantics
- New constants: `uuid.NIL`, `uuid.MAX`

**Python 3.12 and 3.13 do NOT have uuid7 support.**

### 11.2 Third-Party Package Comparison

| Package | Type | ~ops/sec | ARM64 wheels | Notes |
|---------|------|----------|-------------|-------|
| `uuid-utils` | Rust/native (PyO3) | ~2-5M | Yes (`manylinux_aarch64`) | **Recommended**. Drop-in stdlib replacement. Full RFC 9562. |
| `uuid7` | Pure Python | ~200-500K | N/A (pure Python) | Simpler, returns stdlib `uuid.UUID` instances. |
| `python-ulid` | Pure Python | ~200-500K | N/A (pure Python) | ULID format (26-char Crockford Base32), not UUID. |

### 11.3 Recommendation

Use `uuid-utils>=0.9.0`:
- Fastest (Rust-backed native extension)
- Standards-compliant (RFC 9562 UUIDv7)
- Forward-compatible with Python 3.14's `uuid.uuid7()`
- ARM64 pre-built wheels available
- API compatible with `uuid.UUID`

```python
from uuid_utils import uuid7

def generate_session_name() -> str:
    return f"00-session-{uuid7()}"
```

---

## 12. OpenTelemetry with FastAPI

### 12.1 Required Packages

```
opentelemetry-api>=1.27.0
opentelemetry-sdk>=1.27.0
opentelemetry-exporter-otlp-proto-http>=1.27.0   # HTTP exporter (avoids grpcio ARM64 issues)
opentelemetry-instrumentation-fastapi>=0.48b0
```

### 12.2 FastAPI Auto-Instrumentation

The `FastAPIInstrumentor` wraps the ASGI application with middleware:
- Creates a span per HTTP request with semantic convention attributes (`http.method`, `http.url`, `http.status_code`, `http.route`)
- Propagates W3C `traceparent`/`tracestate` context
- Records request duration, active requests metrics
- Sets span status to ERROR on 5xx

```python
FastAPIInstrumentor.instrument_app(
    app,
    excluded_urls="healthz,readyz",
)
```

### 12.3 Setup Pattern (lifespan)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    tracer_provider, meter_provider, logger_provider = setup_telemetry(app)
    yield
    tracer_provider.shutdown()
    meter_provider.shutdown()
    logger_provider.shutdown()
```

### 12.4 Custom Metrics for File Upload

| Metric | Type | Description |
|--------|------|-------------|
| `files.processed` | Counter | Successfully uploaded files |
| `files.failed` | Counter | Failed upload files |
| `upload.duration` | Histogram | Upload time per file (seconds) |
| `file.size` | Histogram | File size (bytes) |
| `queue.depth` | UpDownCounter | Current work queue depth |

### 12.5 Structured JSON Logging with Trace Context

Custom JSON formatter that injects OTel trace context (`trace_id`, `span_id`, `trace_flags`) into log records. Enables log-to-trace correlation in observability backends.

### 12.6 Environment-Based Configuration

OTel SDK respects standard env vars:
```yaml
env:
  - name: OTEL_SERVICE_NAME
    value: "nfs-watcher-uploader"
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://otel-collector:4318"
```

---

## 13. ARM64/Jetson Docker Images

### 13.1 Base Image

**Recommended**: `python:3.12-slim-bookworm` (multi-arch, ~150MB, Debian Bookworm).

No NVIDIA L4T base needed -- this is a pure CPU/IO workload.

### 13.2 ARM64 Package Compatibility

| Package | ARM64 Status |
|---------|-------------|
| `azure-storage-blob` | Pure Python + optional C extensions, works |
| `azure-identity` | Pure Python (`msal`), works |
| `uuid-utils` | Pre-built `manylinux_aarch64` wheels (Rust) |
| `cryptography` (transitive) | v42+ publishes ARM64 wheels |
| `grpcio` | v1.62+ publishes ARM64 wheels (avoided with HTTP exporter) |
| `opentelemetry-*` | Pure Python, works |

### 13.3 Multi-Arch Build

```bash
# Cross-compilation with docker buildx
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag registry/nfs-watcher-uploader:latest \
  --push .

# Native build on Jetson (no buildx needed)
docker build -t nfs-watcher-uploader:latest .
```

### 13.4 Jetson Notes

- JetPack 6.x (L4T R36) uses Ubuntu 22.04 with ARM64 kernel
- k3s installs natively: `curl -sfL https://get.k3s.io | sh -`
- containerd handles multi-arch manifests (pulls `linux/arm64` variant automatically)

---

## 14. k3s NFS PersistentVolume Setup

### 14.1 Approach: Native In-Tree NFS (Recommended)

k3s supports standard NFS volumes without any CSI driver. Requires `nfs-common` on the host:

```bash
sudo apt-get install nfs-common
```

### 14.2 PV/PVC Configuration

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
    server: 192.168.1.100
    path: /export/data
```

### 14.3 k3s-Specific Differences

| Aspect | Standard K8s | k3s |
|--------|-------------|-----|
| Kubelet path | `/var/lib/kubelet` | `/var/lib/rancher/k3s/agent/kubelet` |
| Default storage | None | Local-path provisioner (Rancher) |
| Container runtime | Varies | containerd (built-in) |
| NFS CSI kubeletDir | Default | Must set to `/var/lib/rancher/k3s/agent/kubelet` |

---

## 15. NFS Server on Ubuntu (Jetson)

### 15.1 Setup

```bash
sudo apt-get install nfs-kernel-server
sudo mkdir -p /export/data/incoming /export/data/.processing
sudo chown -R nobody:nogroup /export/data
echo '/export/data 192.168.1.0/24(rw,sync,no_subtree_check,no_root_squash)' | \
  sudo tee -a /etc/exports
sudo exportfs -rav
sudo systemctl enable --now nfs-kernel-server
```

### 15.2 NFS Version Support

`nfs-kernel-server` on modern Ubuntu supports NFSv3, NFSv4.0, NFSv4.1, and NFSv4.2. Client negotiates highest mutual version (typically 4.2 unless restricted by mount options).

### 15.3 NFSv4-Only Configuration

```ini
# /etc/nfs.conf
[nfsd]
vers2=n
vers3=n
vers4=y
vers4.1=y
vers4.2=y
```

### 15.4 Firewall

NFSv4 only needs TCP port 2049:
```bash
sudo ufw allow from 192.168.1.0/24 to any port 2049 proto tcp
```

---

## 16. Multi-Session Kubernetes Patterns

### 16.1 Patterns Evaluated

| Pattern | Description | Recommendation |
|---------|-------------|---------------|
| **A: Simple Deployment** | Single pod, multi-session via API | **Recommended for local Jetson** |
| B: StatefulSet | Ordinal-based session assignment | Overkill for manual sessions |
| C: Job/CronJob | One Job per session | Not designed for interactive sessions |
| D: Operator (CRD) | Custom resource + controller | Massive implementation overhead |

### 16.2 Recommendation

Pattern A (simple Deployment) with multi-session support within a single pod. Rationale:
- Single user, manual API calls
- Local Jetson with single k3s node (horizontal scaling is not meaningful)
- PRD already supports session recovery from `.processing` structure
- Minimal implementation complexity

---

## 17. Azure Async SDK with FastAPI

### 17.1 Client Lifecycle

Use `lifespan` context manager. Create `BlobServiceClient` once at startup. Close **both** client and credential at shutdown.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    credential = DefaultAzureCredential()
    blob_service_client = BlobServiceClient(
        account_url=settings.account_url,
        credential=credential,
    )
    # ... validate, store on app.state ...
    yield
    await blob_service_client.close()
    await credential.close()  # MUST close credential too
```

### 17.2 Key Gotchas

1. **Close both client AND credential** -- credential has its own HTTP session (`ResourceWarning` if leaked)
2. **Do NOT create a new client per request** -- reuse the shared client from `app.state`
3. **Child clients share parent's connection pool** -- no need to close `get_blob_client()` results
4. **Async client is NOT thread-safe** -- only use from asyncio event loop (don't pass to `anyio.to_thread`)
5. **`max_concurrency` uses asyncio tasks, not threads** -- more efficient than sync SDK
6. **NFS I/O still uses `anyio.to_thread`** -- only blob operations are natively async

### 17.3 Large File Upload Pattern

```python
async def upload_file(container_client, local_path, blob_name):
    blob_client = container_client.get_blob_client(blob_name)
    file_size = await anyio.to_thread.run_sync(os.path.getsize, local_path)
    with open(local_path, "rb") as f:
        await blob_client.upload_blob(
            f,
            overwrite=True,
            blob_type="BlockBlob",
            max_concurrency=8,
            length=file_size,
        )
```

---

## References

| Topic | Source |
|-------|--------|
| rename(2) | man7.org/linux/man-pages/man2/rename.2.html |
| nfs(5) | man7.org/linux/man-pages/man5/nfs.5.html |
| inotify(7) | man7.org/linux/man-pages/man7/inotify.7.html |
| NFSv3 | RFC 1813 |
| NFSv4 | RFC 7530, RFC 5661 |
| UUIDv7 | RFC 9562 |
| Azure blob upload | learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-upload-python |
| Azure blob tuning | learn.microsoft.com/en-us/azure/storage/blobs/storage-blobs-tune-upload-download-python |
| DefaultAzureCredential | learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential |
| Azure blob concurrency | learn.microsoft.com/en-us/azure/storage/blobs/concurrency-manage |
| Azure async SDK | learn.microsoft.com/en-us/python/api/azure-storage-blob/azure.storage.blob.aio |
| anyio threads | anyio.readthedocs.io/en/stable/threads.html |
| anyio streams | anyio.readthedocs.io/en/stable/streams.html |
| K8s emptyDir | kubernetes.io/docs/concepts/storage/volumes/#emptydir |
| K8s ephemeral storage | kubernetes.io/docs/concepts/configuration/manage-resources-containers |
| OpenTelemetry Python | opentelemetry.io/docs/languages/python |
| OTel FastAPI instrumentation | opentelemetry-python-contrib.readthedocs.io |
| k3s docs | docs.k3s.io |
| nfs-kernel-server | ubuntu.com/server/docs/network-file-system-nfs |
| Python 3.14 uuid changes | docs.python.org/3.14/library/uuid.html |
