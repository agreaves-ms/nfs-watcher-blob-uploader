# PRD: NFS Watch → Local Stage → Azure Blob Block Upload → Cleanup (FastAPI on Kubernetes)

## 1. Summary

Build a Kubernetes-deployed FastAPI microservice that:

1) Watches an **NFS-mounted** directory for new files (100MB+).
2) **Claims** files safely so only one worker processes them.
3) **Copies** files to local ephemeral storage for stability.
4) Uploads files to **Azure Blob Storage (Block Blob)** with **parallel block/chunk transfers**.
5) Ensures **idempotency** by using stable blob names and **overwriting** on retry.
6) Deletes files from NFS **only after** successful upload.
7) Exposes HTTP endpoints to start/stop watching with an optional **session name** that becomes a folder prefix in Blob Storage.

This service must be reliable, simple, idiomatic Python, and Kubernetes-friendly. Prefer failing fast (crashing the pod) for unrecoverable configuration/auth issues.

---

## 2. Goals

- **Reliably ingest large files** (100MB+) from NFS.
- Keep **FastAPI responsive** (file scanning + uploads must not block the server).
- Provide **idempotent uploads** with overwrite semantics and safe restarts.
- Support **parallel block uploads** using Azure Storage SDK functionality.
- Minimize complexity; avoid orchestration services unless absolutely required.
- Produce logs that help operators diagnose misconfiguration quickly.

---

## 3. Non-goals

- Exactly-once semantics across multiple replicas (we target **at-least-once** with idempotent overwrite).
- Inotify-based file watching (not reliable for NFS).
- A full workflow/orchestrator system (can be added later if needed).
- Complex UI or operator dashboard.

---

## 4. Background / Constraints

### NFS constraints

- NFS may disconnect at any moment.
- File notifications via inotify are not reliable on NFS; implement file discovery via **polling**.
- NFS syscalls can hang if mounts are configured “hard” and server disappears; mitigate by running NFS operations in background threads and rely on Kubernetes probes/restarts if necessary.

### File constraints

- Files are large blocks of files, potentially **100MB+**.
- Filenames are **unique** and already time-sortable.
- Filenames must be used as blob names to preserve restart consistency.

### Azure constraints

- Blob writes must use **Block Blob** uploads (chunked/block-based).
- Blocks/chunks should upload **in parallel**.
- Authentication must prefer **Managed Identity / Workload Identity** (DefaultAzureCredential).
- If managed identity auth fails and storage account credentials are provided, fallback.
- If no viable credentials are present, crash the pod with a clear error.

---

## 5. Users & Use Cases

### Primary user

- Platform/infra engineer or service owner who deploys and operates the ingestion service in Kubernetes.

### Key use cases

1. Start a new ingestion run with a session name.
2. Start a run without a session name (auto-session).
3. Stop scanning (finish in-flight work).
4. Recover from pod restarts without losing progress or corrupting blobs.
5. Diagnose why uploads are failing (auth misconfig, NFS outage, etc.).

---

## 6. Requirements

### 6.1 Functional requirements (FR)

#### FR-1: Watch and scan NFS directory (polling)

- Service must poll an NFS directory (e.g., `/mnt/nfs/incoming`) every `poll_interval_s`.
- Service must detect “stable” files before ingesting:
  - File must be older than `min_file_age_s` **and**
  - File’s `(size, mtime)` must match in two consecutive scans.

#### FR-2: Non-blocking server behavior

- Scanning and all filesystem/network I/O must not block FastAPI request handling.
- Implementation must run NFS and upload operations in background workers (threads via anyio/to_thread or equivalent).

#### FR-3: Claiming / locking to avoid double processing

- For each stable file discovered, the service must **atomically claim** it by renaming:
  - from: `/mnt/nfs/incoming/<filename>`
  - to: `/mnt/nfs/.processing/<date>/<encoded_session>/<filename>`
- Renaming must be atomic (same filesystem) and should serve as a distributed lock.
- If rename fails because file doesn’t exist or already moved, it must be treated as a benign race and skipped.

#### FR-4: Copy to local staging

- After claim, file must be copied to local ephemeral storage:
  - `/mnt/staging/<date>/<encoded_session>/<filename>`
- Local copy must fsync or otherwise ensure file data is durable locally before upload begins.

#### FR-5: Azure Blob upload (Block Blob, parallel chunks)

- Service must upload the local file to Azure Blob Storage as **Block Blob**.
- Upload must use SDK-supported parallelism (chunk/block uploads in parallel).
- Upload must use blob name:
  - `/<date>/<encoded_session>/<filename>` (no extra hashing or renaming)
- Overwrite must be enabled to provide idempotency:
  - If the blob exists (including partial or previous run), overwrite it.

#### FR-6: Cleanup rules

- NFS file must **NOT** be deleted until upload succeeded.
- After successful upload:
  - Delete the claimed file from `.processing/...`
  - Delete local staging file.

#### FR-7: Session support via API

- `POST /v1/watch/start` must accept optional `session_name`.
- If `session_name` omitted or blank:
  - default to `00-session-<sortable-uuid>` where the prefix makes it time-sortable.
- Session name must be URL-encoded for folder safety:
  - `encoded_session = url_encode(session_name)`
- The date prefix must be captured at session start:
  - `date = current UTC date as YYYYMMDD` (sortable date)
- Blob prefix must be:
  - `<date>/<encoded_session>/`

#### FR-8: Recovery across restarts

- On startup, the service must scan:
  - `/mnt/nfs/.processing/**`
- Any files found there must be enqueued for upload using the date/session derived from the directory path.
- This ensures session/date remain stable after restarts.

#### FR-9: Authentication behavior (fail-fast)

- Prefer Managed Identity / Workload Identity via `DefaultAzureCredential`.
- At startup, attempt to authenticate and “touch” the blob service to validate credentials.
- If managed identity auth fails and fallback creds are provided, attempt fallback.
- If no viable auth method exists, raise an exception and crash the pod.
- Logs must clearly indicate what config keys are missing.

---

### 6.2 Non-functional requirements (NFR)

#### NFR-1: Simplicity / maintainability

- Favor straight-line logic and avoid high cyclomatic complexity.
- Use standard, well-known Python modules and Azure SDK.
- Avoid implementing custom block upload logic unless required; rely on SDK.

#### NFR-2: Reliability

- Bounded in-memory queue to avoid memory exhaustion under load.
- Worker pool concurrency configurable.
- If NFS errors are transient, watcher should backoff and retry.
- If unrecoverable errors occur (e.g., auth misconfig), crash the pod.

#### NFR-3: Performance

- Parallel chunk uploads enabled and configurable.
- Local staging reduces NFS read flakiness and improves upload stability.

#### NFR-4: Security

- Prefer Managed Identity / Workload Identity (no secrets).
- If fallback uses account keys/connection strings, they must be injected via Kubernetes secrets.
- Avoid writing secrets to logs.

#### NFR-5: Observability

- Structured-ish logging with file/session/date context.
- Provide status endpoint with counters and last error string.

---

## 7. API Specification

### 7.1 Endpoints

#### `GET /healthz`

- Always returns `{ "ok": true }` if process is alive.

#### `GET /readyz`

- Returns `{ "ready": true }` if startup initialization completed.
- Must remain simple; Azure auth validation happens at startup and will crash if broken.

#### `POST /v1/watch/start`

- Starts scanning and sets the active session.
- Request body:

  ```json
  { "session_name": "optional string" }
  ```

- Response body:

  ```json
  {
    "date_prefix": "YYYYMMDD",
    "session_name": "original or generated",
    "encoded_session": "urlencoded"
  }
  ```

#### `POST /v1/watch/stop`

- Stops scanning; workers may continue to process items already queued.
- Response: `{ "enabled": false }`

#### `GET /v1/status`

- Returns:

  ```json
  {
    "enabled": true,
    "active_session": "string or null",
    "processed_ok": 0,
    "processed_err": 0,
    "last_error": "string or null"
  }
  ```

---

## 8. Data / Folder Structures

### 8.1 NFS structure

- Incoming (watched):

  - `/mnt/nfs/incoming/<filename>`

- Processing (claimed):

  - `/mnt/nfs/.processing/YYYYMMDD/<encoded_session>/<filename>`

### 8.2 Local staging (ephemeral)

- `/mnt/staging/YYYYMMDD/<encoded_session>/<filename>`

### 8.3 Blob structure

- Container: `APP_AZURE_CONTAINER`
- Blob name:

  - `YYYYMMDD/<encoded_session>/<filename>`

---

## 9. High-level Architecture

### Components

1. **FastAPI app** with endpoints to control sessions.
2. **Polling watcher** task:

   - scans NFS incoming directory at an interval
   - identifies stable files
   - enqueues candidates
3. **Worker pool** (N workers):

   - claim (atomic rename)
   - copy to local
   - upload to Azure with parallel chunk transfers
   - cleanup
4. **Recovery step on startup**:

   - scan `.processing/**` and enqueue anything found

### Concurrency Model

- FastAPI runs on an asyncio event loop.
- All blocking I/O is executed in background threads via `anyio.to_thread`.
- A bounded `anyio` memory channel acts as the work queue.

---

## 10. Detailed Processing Flow

### Startup

1. Read config from environment.
2. Initialize Azure client:

   - Attempt DefaultAzureCredential.
   - If fails and fallback provided, use fallback.
   - Else raise (crash).
3. Ensure required directories exist.
4. Recover `.processing/**` files into queue.
5. Start background watcher + worker tasks.

### Session Start

1. Determine `session_name`:

   - use provided string if non-empty else generate `00-session-<sortable-uuid>`.
2. Compute `date_prefix` as `YYYYMMDD` in UTC.
3. URL-encode session name to `encoded_session`.
4. Ensure session directories exist on NFS + local.
5. Set session active and enable watcher.

### Session Stop

- Disable watcher (stop scanning) but allow in-flight processing.

### Per-file pipeline

1. Watcher discovers stable file in incoming.
2. Worker tries to claim by atomic rename into `.processing/<date>/<session>/`.
3. Copy claimed file to local staging.
4. Upload local file to Azure blob:

   - `overwrite=True`
   - `blob_type="BlockBlob"`
   - parallel chunk transfers enabled via SDK concurrency settings
5. Delete claimed file from `.processing`.
6. Delete local staging file.

### Crash / restart behavior

- If pod crashes mid-upload:

  - NFS file is still in `.processing`, so recovery will re-enqueue it.
  - Blob upload overwrites previous partial blob on retry.

---

## 11. Configuration

### Environment variables (all prefixed with `APP_`)

#### Required

- `APP_AZURE_ACCOUNT_URL`
  Example: `https://<account>.blob.core.windows.net`
- `APP_AZURE_CONTAINER`
  Example: `ingest`

#### Optional (fallback auth)

- `APP_AZURE_CONNECTION_STRING`
- OR

  - `APP_AZURE_ACCOUNT_NAME`
  - `APP_AZURE_ACCOUNT_KEY`

If DefaultAzureCredential fails and no fallback is set, the service must crash.

#### NFS paths

- `APP_NFS_INCOMING_DIR` (default `/mnt/nfs/incoming`)
- `APP_NFS_PROCESSING_ROOT` (default `/mnt/nfs/.processing`)

#### Local staging

- `APP_LOCAL_STAGING_ROOT` (default `/mnt/staging`)

#### Watcher tuning

- `APP_POLL_INTERVAL_S` (default 2.0)
- `APP_MIN_FILE_AGE_S` (default 5.0)

#### Queue and workers

- `APP_MAX_QUEUE_SIZE` (default 2000)
- `APP_WORKER_CONCURRENCY` (default 4)

#### Azure upload tuning

- `APP_AZURE_MAX_BLOCK_SIZE` (default 8MiB)
- `APP_AZURE_MAX_SINGLE_PUT_SIZE` (default 4MiB)
- `APP_AZURE_MAX_CONCURRENCY` (default 8)

---

## 12. Kubernetes Deployment Requirements

### Volumes

- NFS PVC mounted at `/mnt/nfs`
- emptyDir mounted at `/mnt/staging`

### Probes

- Liveness: `GET /healthz`
- Readiness: `GET /readyz`

### Replicas

- Default `replicas: 1` (recommended).
  Multiple replicas may cause duplicate uploads but overwrite/idempotency prevents corruption; can still waste bandwidth.

### Mount options

- Prefer configuring NFS mount options at PV level (`mountOptions`) if required by ops policy.

---

## 13. Error Handling & Retry Policy

### Fail-fast errors (crash pod)

- Missing required Azure config (e.g., account URL, container).
- Authentication fails with DefaultAzureCredential and no fallback creds present.
- Container client initialization fails.

### Transient errors (retry/backoff)

- NFS transient errors during scan (e.g., stale handles, timeouts) should trigger exponential backoff in watcher loop.
- Upload errors: allow SDK built-in retries where present; optionally add a small outer retry policy around upload if needed, but avoid complexity.

### Poison files

- If a file repeatedly fails upload, it remains in `.processing` and will be retried after restart.
- Optional future enhancement: move to `.failed/` after N attempts (out of scope unless required).

---

## 14. Observability & Logging

### Logging

- Log at INFO:

  - session start/stop
  - recovery counts
  - per-file success (optional; can be noisy)
- Log at WARNING:

  - transient NFS issues
- Log at ERROR/EXCEPTION:

  - worker failures with file/session/date context
  - startup auth failures

### Status metrics (simple counters)

- `processed_ok` (monotonic)
- `processed_err` (monotonic)
- `last_error` (string)

---

## 15. Implementation Plan (Milestones)

### M1: Skeleton service

- FastAPI app + endpoints
- Background task wiring (watcher + workers)
- Session handling

### M2: File pipeline

- Stable-file polling logic
- Claim/rename + local copy + cleanup

### M3: Azure upload (Block Blob, parallel)

- AzureUploader with DefaultAzureCredential and fallback
- Upload tuning parameters
- Overwrite behavior

### M4: Recovery + operational polish

- Scan `.processing/**` on startup
- Logging improvements
- Kubernetes manifests + probes

### M5: Hardening (optional)

- Backpressure tuning for queue
- Optional retry policy around upload
- Optional `.failed/` quarantine

---

## 16. Acceptance Criteria

1. Starting a session with no name creates blob folder:

   - `YYYYMMDD/00-session-<sortable-uuid>/`
2. Starting a session with name `"my session"` creates blob folder:

   - `YYYYMMDD/my%20session/`
3. For each file in NFS incoming:

   - It is moved to `.processing/YYYYMMDD/<session>/`
   - Copied to local staging
   - Uploaded as Block Blob with overwrite enabled
   - Deleted from NFS only after successful upload
4. If pod is killed mid-upload:

   - File remains in `.processing`
   - On restart, recovery enqueues and upload overwrites blob cleanly
5. FastAPI remains responsive while processing large files.
6. If Azure auth is misconfigured:

   - Pod crashes at startup and logs clearly indicate missing config/credentials.

---

## 17. Example Repository Structure

```text
app/
  main.py
  config.py
  models.py
  session.py
  watcher.py
  processor.py
  azure_uploader.py
  service.py
pyproject.toml
Dockerfile
k8s/
  deployment.yaml
  pvc.yaml
  pv.yaml
```

---

## 18. Appendix: Key Design Notes

- Polling is used instead of inotify due to NFS semantics.
- Local staging reduces NFS flakiness during long reads and provides stable upload source.
- Atomic rename is used as a claim mechanism.
- Overwrite uploads provide safe idempotency and restart behavior.
- Authentication prefers Managed Identity / Workload Identity (DefaultAzureCredential) and only falls back to explicit account credentials if provided.
