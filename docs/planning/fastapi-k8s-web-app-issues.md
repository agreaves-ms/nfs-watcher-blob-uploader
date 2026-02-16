# Identified Issues & Considerations: NFS Watch FastAPI K8s Web App

> This document tracks architectural, functional, and non-functional issues identified during PRD review and research.
>
> Legend: `[x]` = resolved/closed, `[~]` = accepted risk

---

## Architectural Issues

- [x] **AI-1: NFS rename atomicity depends on NFS version** -- With NFSv4.1 configured on both server (`nfs-kernel-server`) and client (`nfsvers=4.1` mount option), rename has exactly-once semantics (EOS). Catch `errno.ESTALE` alongside `errno.ENOENT` in the rename call -- both mean "file already gone, skip it." Two lines of error handling, not an architectural concern.

- [x] **AI-2: Single-session model limits operational flexibility** -- 409 Conflict on duplicate `POST /v1/watch/start`. Multiple replicas handle different sessions. Resolved by user answers (A1, A11).

- [x] **AI-3: Recovery path assumes directory structure encodes session metadata** -- The `.processing/YYYYMMDD/encoded_session/filename` path is deterministic. On startup, walk the directory tree, parse path components, re-enqueue all files found. No "most recent" heuristic needed -- just process everything in `.processing/`. If files belong to different sessions, the blob path is already encoded in the directory structure, so uploads land in the right place regardless.

- [x] **AI-4: anyio.to_thread for NFS I/O may exhaust thread pool** -- With the async Azure SDK, only NFS operations use threads (scandir, rename, copy). At `WORKER_CONCURRENCY=4` plus one watcher, peak thread usage is ~5 concurrent NFS calls against a default 40-thread pool. A separate `CapacityLimiter` adds complexity for a problem that doesn't exist at this scale. Use the default thread pool. Set `abandon_on_cancel=True` on NFS calls so the event loop isn't blocked if NFS hangs (the pod will die from liveness probe anyway).

- [x] **AI-5: emptyDir for local staging has size limits and eviction risk** -- 4TB node storage. 100Gi sizeLimit. Worst-case 40GB concurrent staging. Not a concern.

- [x] **AI-6: Queue size vs staging capacity mismatch** -- Queue holds metadata (file paths), not file data. 2000 entries is fine.

- [x] **AI-7: PRD blob upload defaults diverge from Azure SDK defaults** -- Use SDK defaults. User confirmed PRD values were arbitrary.

- [x] **AI-8: DefaultAzureCredential chain exhaustion takes 30+ seconds** -- Accepted. Startup probe covers the window.

- [x] **AI-9: Sync vs async Azure SDK client choice** -- Use async SDK (`azure.storage.blob.aio`).

- [x] **AI-10: anyio memory channel clone semantics require careful lifecycle management** -- Replace with `asyncio.Queue(maxsize=N)`. Built into the standard library, supports multiple concurrent consumers natively (multiple workers calling `queue.get()`), provides backpressure via `maxsize`, and has no clone lifecycle to manage. anyio memory channels are more complex than needed for a single-producer/multi-consumer work queue. `asyncio.Queue` is the simplest correct solution.

- [x] **AI-11: Multi-session architecture is a fundamental PRD change** -- Each session watches `/mnt/nfs/incoming/{{session}}/`. `POST /v1/watch/start` accepts session name (which doubles as the NFS subfolder name). Watcher polls that subfolder. Pod handles one session; multiple pods handle multiple sessions. Straightforward.

- [x] **AI-12: File completion marking + GC is a new component not in the PRD** -- After successful upload, rename `file.dat` to `file.dat.completed` on NFS. Background asyncio task scans for `.completed` files and deletes them. Delete immediately (no retention period). If NFS is down, GC silently skips; the pod will die from liveness probe shortly anyway. One simple background loop.

- [x] **AI-13: Configurable file extension filtering is a new requirement** -- `APP_FILE_EXTENSIONS=".bin,.mp4,.dat"` parsed into a `frozenset` at startup. Filter applied in the scandir loop with `Path(name).suffix in allowed_extensions`. If env var is empty/unset, accept all files. Three lines of code.

- [x] **AI-14: ARM64/Jetson deployment target affects container build** -- `python:3.12-slim-bookworm` supports multi-arch. All dependencies publish ARM64 wheels. Standard `docker build` on Jetson produces ARM64 images natively. No special handling needed.

- [x] **AI-15: Session auto-resume on startup changes startup flow** -- On startup, scan `.processing/` for existing directories. If found, derive session name from path, start watcher for the corresponding incoming subfolder. This is just the recovery step (already in the PRD) extended to also resume the watcher. A few extra lines in the startup sequence.

- [x] **AI-16: NFS server runs on the same local network as k3s** -- Low-latency NFS. If NFS goes down, pod dies via liveness probe, K8s restarts it. No special handling.

---

## Functional Issues

- [x] **FI-1: URL encoding of session names may create ambiguous blob paths** -- Validate session names on input: allow `[a-zA-Z0-9_\-.]` only, reject everything else with 400 Bad Request. No URL encoding needed. The session name IS the NFS subfolder name and the blob path component. This is a shim service; restrictive input validation is simpler and safer than encoding/decoding.

- [x] **FI-2: File stability check has a race window and NFS caching interaction** -- With `actimeo=5` on the mount and `min_file_age_s=5`, the attribute cache is guaranteed fresh by the time the stability check passes. Document this dependency in the K8s manifest comments. Not a code concern.

- [x] **FI-3: No mechanism to detect partial/corrupt files from NFS writer** -- Files are considered complete when stable (two consecutive scans with matching `(size, mtime)`). The writer is expected to write atomically (write to temp, rename into incoming). If the writer doesn't do this, the stability check is the safety net. Accepted.

- [x] **FI-4: Cleanup order may leave orphaned staging files** -- emptyDir is ephemeral. On pod restart, staging is wiped clean. Orphaned staging files are harmless and short-lived.

- [x] **FI-5: Recovery does not handle orphaned local staging files** -- emptyDir is ephemeral. Not an issue.

- [x] **FI-6: Date prefix captured at session start creates edge-case at midnight UTC** -- Accepted. Long-running sessions keep their original date prefix. By design.

- [x] **FI-7: `POST /v1/watch/stop` behavior with queued items is underspecified** -- Stop immediately. Queued items stay in `.processing` and get re-enqueued on next start. Everything is idempotent.

- [x] **FI-8: No health degradation signal for NFS disconnection** -- Pod dies on NFS failure. `hard` mount hangs threads, liveness probe fails (pure async, unaffected by D-state threads), kubelet kills the pod. K8s restarts it. No health check thread or circuit breaker needed.

- [x] **FI-9: `fsync` on local copy may not guarantee durability on emptyDir** -- Everything is idempotent. If the local copy is lost, the file is still in `.processing` on NFS and gets re-copied on restart. `fsync` is best-effort, not required for correctness.

- [x] **FI-10: GC process needs configurable retention and error handling** -- Delete `.completed` files immediately (no retention). If NFS is down, GC skips silently. One config value: `APP_GC_INTERVAL_S=30`. Keep it simple; add retention later if needed.

- [x] **FI-11: Session subfolder structure changes the NFS incoming directory layout** -- Watcher watches `/mnt/nfs/incoming/{{session}}/` where `{{session}}` is the validated session name from `POST /v1/watch/start`. The NFS writer must place files in the correct session subfolder. Recovery scans `.processing/` to map files back to their session.

---

## Non-Functional Issues

- [x] **NI-1: Observability should use OpenTelemetry** -- `opentelemetry-instrumentation-fastapi` for automatic HTTP spans. OTLP HTTP exporter (avoids `grpcio` on ARM64). Custom counters/histograms for file processing. Standard setup.

- [x] **NI-2: No rate limiting on API endpoints** -- Not applicable. Secure local network.

- [x] **NI-3: No graceful shutdown handling specified** -- Immediate death on SIGTERM. Everything is idempotent.

- [x] **NI-4: Structured logging format not specified** -- JSON structured logging via Python `logging` + OTel log bridge for trace context injection (`trace_id`, `span_id`). Standard pattern, well-documented in OTel Python docs.

- [x] **NI-5: No resource requests/limits specified for K8s deployment** -- Provide sensible defaults in manifests. Let the user tune for their specific Jetson model. Streaming file I/O (not loading entire files into memory) keeps memory usage bounded regardless of file size.

- [x] **NI-6: No TLS/mTLS consideration** -- Not applicable. Secure local network.

- [x] **NI-7: Upload bandwidth could saturate network** -- Bounded by Jetson uplink, not service parallelism. User tunes `APP_WORKER_CONCURRENCY` and `APP_AZURE_MAX_CONCURRENCY` if needed. Accepted.

- [x] **NI-8: NFS mount options not specified in K8s manifests** -- Specified in design decisions: `hard,nfsvers=4.1,actimeo=5,rsize=1048576,wsize=1048576`. Provided in PV manifest.

- [x] **NI-9: No startup probe specified** -- `failureThreshold=24, periodSeconds=5` (2 min window) covers DefaultAzureCredential chain exhaustion and NFS mount delays.

- [x] **NI-10: Liveness probe failureThreshold not specified** -- `failureThreshold=3, periodSeconds=10` (30s). Aggressive enough to kill the pod on NFS hang, lenient enough to avoid false positives.

- [x] **NI-11: k3s-specific deployment considerations** -- Native in-tree NFS volumes (no CSI driver). `nfs-common` installed on host. kubeletDir difference (`/var/lib/rancher/k3s/agent/kubelet`) is only relevant for CSI drivers, which we don't use. Standard manifests work.

- [x] **NI-12: Jetson hardware constraints affect resource sizing** -- Don't over-specify. Streaming I/O keeps memory bounded. Provide defaults, let user tune. The service is I/O-bound (NFS + network), not CPU-bound.

---

*All issues resolved. Ready for implementation planning.*
