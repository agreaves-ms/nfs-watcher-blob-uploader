# Questions for Clarification: NFS Watch FastAPI K8s Web App

> Questions requiring user interpretation or direction before finalizing the design. Updated progressively.

---

## Session Management

- [ ] **Q1: What should happen if `POST /v1/watch/start` is called while a session is already active?** Options: (a) Return error 409 Conflict, (b) Stop old session and start new one, (c) Allow multiple concurrent sessions. This has significant architectural implications.
- A1: Option (a)

- [ ] **Q2: Should sessions persist across pod restarts?** The recovery mechanism re-enqueues `.processing` files, but the session metadata (name, date) is derived from directory paths. Should the service also restore the "active session" state, or does a restart require a new `POST /v1/watch/start` call?
- A2: Start-up should always resume the active session if there is one in progress.

- [ ] **Q3: Is the `00-session-<sortable-uuid>` format for auto-generated session names finalized?** What makes the UUID "sortable" - is this UUIDv7, ULID, or a custom format? This affects sort order in blob storage.
- A3: Whichever fastest sortable uuid module/function already exists.

---

## File Handling

- [ ] **Q4: What is the expected maximum file size?** The PRD says "100MB+" but doesn't specify an upper bound. This affects: emptyDir sizing, upload timeout configuration, and memory planning for concurrent processing.
- A4: 10GB

- [ ] **Q5: What is the expected file throughput (files/hour)?** This determines whether `APP_MAX_QUEUE_SIZE=2000` is appropriate and helps size the worker pool. Is the workload bursty or steady?
- A5: Bursty, the user will use a local app to call the watch/start endpoint, files will already exist on the NFS and/or new files begin to appear on the NFS.

- [ ] **Q6: Are there any file types that should be excluded (e.g., temp files, hidden files, partial writes with specific naming patterns)?** The PRD watches all files in the incoming directory without filtering.
- A6: This should be configurable which file types based on extension should be considered for file upload.

- [ ] **Q7: How does the NFS writer signal that a file is complete?** The PRD relies on size/mtime stability detection. Does the writer close the file handle, use a rename-to-final-name pattern, or is the polling-based stability check the only mechanism available?
-A7: File move as an atomic operations, files should be moved to include completed extensions, there should be a garbage collection process that runs continually and deletes these completed files, both from the NFS and locally.

---

## Azure / Blob Storage

- [ ] **Q8: Should the service create the Azure blob container if it doesn't exist, or should it fail?** The PRD doesn't specify container creation behavior. Managed Identity may not have container-create permissions.
- A8: Container creation otherwise fail error if non-existent or missing permissions on creation.

- [ ] **Q9: Is there a blob retention/lifecycle policy in place on the target container?** This affects whether the service needs to handle blob expiry or if that's managed externally.
- A9: No blob retention/lifecycle or expiry managed by the app.

- [ ] **Q10: What Azure regions/endpoints are targeted?** Latency to blob storage affects upload concurrency tuning and timeout settings. Is this a single-region deployment?
- A10: Shouldn't be a concern.

---

## Kubernetes / Deployment

- [ ] **Q11: Will this service ever run with more than 1 replica?** The PRD says "recommended replicas: 1" but allows multiple. If multi-replica is a real use case, the atomic-rename claim mechanism needs more scrutiny, and we should consider a proper distributed lock.
- A11: No distributed lock. Multiple replicas and file watcher/file uploads could be done for multiple **different** sessions. Sessions will add their files under a {{session}} folder in the NFS. Provide suggestions as well, should we use a StatefulSet and have the sessions "assigned" to a specific pod id, what other ideas do you have?

- [ ] **Q12: What NFS server and version is being used?** NFSv3 vs NFSv4.1+ has significant implications for rename atomicity and retry safety. Research confirms NFSv4.1+ provides exactly-once semantics (EOS) for retried rename RPCs, while NFSv3 does not. Is this Azure NetApp Files, Azure Files NFS, or a custom NFS server? Azure NetApp Files supports both v3 and v4.1; Azure Files NFS is v4.1 only; Azure Blob NFS is v3 only with limitations (no file locking).
- A12: Whatever we can install and run on a local Jetson computer running Ubuntu.

- [ ] **Q13: What NFS mount options are currently configured (or planned)?** Research shows the default `acregmax=60s` means `stat()` results could be stale for up to 60 seconds. If the NFS mount uses defaults, `min_file_age_s=5` (PRD default) is insufficient for reliable stability detection -- it should be >= `acregmax`. What `actimeo`/`acregmax` values will the PV use?
- A13: Nothing configured or specified, provide suggestions. NFS could be disconnected or reconnected at any time, when watcher/start is called then the expectation is that the NFS is on and available, it could die in which case the file watcher should probably die and attempt to resume until the NFS is re-attached using kubernetes primatives (kill the pod and starting back up, etc, avoiding building circuit breakers and such from scratch use whats available from kubernetes).

- [ ] **Q14: Is there a service mesh (Istio, Linkerd) or network policy in the target cluster?** This affects how the FastAPI endpoints are exposed and whether mTLS is handled externally.
- A14: Nothing specified, no need for mTLS.

---

## Operations / Resilience

- [ ] **Q15: What is the desired behavior for "poison files" that repeatedly fail upload?** The PRD mentions an optional `.failed/` quarantine as out-of-scope. Should this be in-scope for the initial release, or is manual intervention acceptable?
- A15: Out of scope.

- [ ] **Q16: Should the service expose Prometheus metrics?** NFR-5 is ambiguous about monitoring integration. Standard K8s deployments typically use Prometheus. Should we include a `/metrics` endpoint?
- A16: Sure using OTel collectors.

- [ ] **Q17: Is there a log aggregation system the structured logs should target?** This determines whether to use JSON logging, specific field names, or correlation IDs.
- A17: OTel collectors.

- [ ] **Q18: What is the expected graceful shutdown behavior?** Should SIGTERM cause: (a) immediate stop of watcher + drain in-flight work, (b) immediate stop of everything, or (c) configurable grace period for in-flight uploads?
- A18: Immediate death, everything needs to be idempotent

---

## Security

- [ ] **Q19: Should the API endpoints require authentication?** Currently any pod with network access can start/stop sessions. Is network policy sufficient, or do we need API key / bearer token auth?
- A19: No

- [ ] **Q20: Are there compliance requirements for the data being transferred?** This affects logging (should filenames be logged?), encryption at rest, and transit encryption requirements.
- A20: No

---

## Technical Design Decisions (from research)

- [ ] **Q21: Should the service use the sync or async Azure Storage SDK?** The async SDK (`azure.storage.blob.aio`) uses asyncio natively and would avoid consuming thread pool tokens for blob uploads. The sync SDK requires `anyio.to_thread` wrappers. The async SDK adds complexity but saves thread pool resources. Which approach is preferred?
- A21: Whatever is bestest and modernest

- [ ] **Q22: Should `DefaultAzureCredential` be replaced with a more targeted credential chain?** Research shows the full DefaultAzureCredential chain takes 30+ seconds to fail. In production, Microsoft recommends using specific credentials. Options: (a) Keep DefaultAzureCredential for simplicity, (b) Use `ChainedTokenCredential` with only WorkloadIdentity + fallback, (c) Use `AZURE_TOKEN_CREDENTIALS=prod` environment variable. Which approach?
- A22: Keep DefaultAzureCredential but make sure the logic is goodest and idiomatic-est.

- [ ] **Q23: Are the PRD's Azure upload tuning defaults intentional?** The PRD specifies `max_single_put_size=4MiB` and `max_block_size=8MiB`. The Azure SDK defaults are 64MiB and 4MiB respectively. The PRD's lower `max_single_put_size` forces chunked upload for most files, and the larger `max_block_size` produces fewer but larger blocks. Were these values chosen deliberately, or should we use SDK defaults?
- A23: No they're random, determine what they should be, can we ignore the tuning why do we care?

- [ ] **Q24: What is the expected node ephemeral storage capacity?** This determines the safe `sizeLimit` for the emptyDir staging volume and the `ephemeral-storage` resource limits. How much local disk space is available on target nodes?
- A24: 4TB

- [ ] **Q25: Should the watcher pause during graceful shutdown?** Research shows K8s SIGTERM starts the `terminationGracePeriodSeconds` countdown. Should the watcher stop immediately on SIGTERM (stop enqueuing new items) while workers drain in-flight uploads? What `terminationGracePeriodSeconds` should be configured based on expected upload times?
- A25: yeah sure, whatever requires the least amount of code, it throwing an error and the pod dying is perfectly fine too.

- A26: This kubernetes will be k3s and will be running as a local node cluster, it will not receive heavy traffic, it will be behind a secure network where the cluster will not be accessible from the internet.

---

*Last updated: 2026-02-15 - Updated with research-informed technical design questions*
