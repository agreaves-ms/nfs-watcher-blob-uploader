# NFS Watcher Blob Uploader

A FastAPI service that watches an NFS mount for new files and uploads them to Azure Blob Storage. It's built for high-throughput ingest pipelines where files land on a shared NFS volume and need to be reliably transferred to cloud storage.

The service handles the full lifecycle: detecting new files as they appear, claiming them so multiple instances don't collide, staging them locally for fast reads, uploading to Azure Blob, and cleaning up afterward. If the process crashes mid-upload, it picks up where it left off on restart.

## How it works

```
Files land on NFS ──> Watcher polls ──> Queue ──> Worker pool ──> Azure Blob
                         │                            │
                      (stable?)                  claim ──> stage ──> upload ──> cleanup
                         │
                      enqueue
```

1. An external process (or the test file generator) drops files into `/mnt/nfs/incoming/<session>/`.
2. The **watcher** polls the directory, checks that each file's size and mtime haven't changed across two consecutive scans, and waits for a configurable minimum age. This avoids uploading files that are still being written.
3. Stable files are enqueued and picked up by **workers**, which:
   - **Claim** the file with an atomic `rename` into `.processing/` (prevents double-processing across replicas)
   - **Copy** it to local staging with `fsync` (avoids repeated NFS reads during upload)
   - **Upload** it to Azure Blob Storage
   - **Mark** it as `.completed` and delete the staging copy
4. A **GC loop** periodically sweeps `.completed` files and prunes empty directories.
5. On startup, **recovery** scans `.processing/` for files that were interrupted and re-enqueues them.

## Directory layout at runtime

```
/mnt/nfs/
  incoming/<session>/        # new files land here
  .processing/<date>/<session>/
    file.bin                 # claimed, being uploaded
    file.bin.completed       # done, awaiting GC

/mnt/staging/<date>/<session>/
  file.bin                   # local copy, transient
```

Blobs land in Azure at `<container>/<YYYYMMDD>/<session>/<filename>`.

## Prerequisites

- Python 3.12+
- Docker and Docker Compose
- For Kubernetes deployment: a cluster with NFS PV access and an Azure Storage account

## Local development

Copy the example env file and start the dev dependencies (Azurite + OpenTelemetry Collector):

```bash
cp .env.example .env
make install
make dev-up
```

Then run the app on your host with hot reload:

```bash
make run
```

The app starts at `http://localhost:8000`. Azurite provides a local Azure Storage emulator on port 10000, and the OTel Collector receives telemetry on ports 4317/4318.

Stop the dev containers when you're done:

```bash
make dev-down
```

## Docker Compose (full stack)

This brings up everything in containers: the app, Azurite, the OTel Collector, and a test file generator.

```bash
make docker-up
```

Four services start:

| Service | Port | Description |
|---|---|---|
| `app` | 8000 | The watcher/uploader |
| `azurite` | 10000 | Azure Storage emulator |
| `otel-collector` | 4317, 4318 | Telemetry collector |
| `test-nfs` | 8080 | Test file generator |

Tear it down:

```bash
make docker-down
```

## Running an end-to-end test

With the full stack running (`make docker-up`), use the Makefile targets to drive a test:

```bash
# Start a watch session
make watch-start

# Start generating files (10 x 64KB files, one every 2s)
make gen-start

# Check progress
make test-status

# Stop when done
make gen-stop
make watch-stop
```

You can customize the file generation:

```bash
make gen-start SESSION=my-run INTERVAL=1 SIZE=1048576 COUNT=50
```

| Variable | Default | Description |
|---|---|---|
| `SESSION` | `test-session` | Session name |
| `INTERVAL` | `2` | Seconds between files |
| `SIZE` | `65536` | File size in bytes |
| `COUNT` | `10` | Number of files (0 = unlimited) |

## API

### Main app (port 8000)

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness probe |
| `GET` | `/readyz` | Readiness probe (503 until startup completes) |
| `POST` | `/v1/watch/start` | Start a watch session |
| `POST` | `/v1/watch/stop` | Stop the active session |
| `GET` | `/v1/status` | Session state and processing counters |

Start a session:

```bash
curl -X POST http://localhost:8000/v1/watch/start \
  -H 'Content-Type: application/json' \
  -d '{"session_name": "my-session"}'
```

If `session_name` is omitted, one is auto-generated using UUIDv7.

### Test file generator (port 8080)

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness probe |
| `POST` | `/v1/generate/start` | Start generating files |
| `POST` | `/v1/generate/stop` | Stop generation |
| `GET` | `/v1/generate/status` | Generator state and file count |

## Configuration

All settings use the `APP_` environment variable prefix.

| Variable | Default | Description |
|---|---|---|
| `APP_AZURE_ACCOUNT_URL` | *(required)* | Azure Blob Storage account URL |
| `APP_AZURE_CONTAINER` | *(required)* | Target blob container name |
| `APP_AZURE_CONNECTION_STRING` | `None` | Fallback auth via connection string |
| `APP_AZURE_ACCOUNT_NAME` | `None` | Fallback auth account name |
| `APP_AZURE_ACCOUNT_KEY` | `None` | Fallback auth account key |
| `APP_NFS_INCOMING_DIR` | `/mnt/nfs/incoming` | Directory where new files appear |
| `APP_NFS_PROCESSING_ROOT` | `/mnt/nfs/.processing` | In-progress processing directory |
| `APP_LOCAL_STAGING_ROOT` | `/mnt/staging` | Local staging before upload |
| `APP_POLL_INTERVAL_S` | `2.0` | Watcher poll interval (seconds) |
| `APP_MIN_FILE_AGE_S` | `5.0` | Minimum file age before pickup |
| `APP_FILE_EXTENSIONS` | *(empty, all files)* | Comma-separated filter (e.g. `.bin,.mp4`) |
| `APP_WORKER_CONCURRENCY` | `4` | Number of concurrent upload workers |
| `APP_MAX_QUEUE_SIZE` | `2000` | Work queue capacity |
| `APP_AZURE_MAX_CONCURRENCY` | `8` | Azure SDK upload concurrency per file |
| `APP_GC_INTERVAL_S` | `30.0` | GC sweep interval |

Authentication tries `DefaultAzureCredential` first (managed identity, Azure CLI, env vars), then falls back to connection string or account key. In Docker Compose, the Azurite connection string is preconfigured.

OpenTelemetry is controlled by standard OTel variables:

| Variable | Description |
|---|---|
| `OTEL_SERVICE_NAME` | Service name for traces and metrics |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector endpoint. Unset to disable export. |

## Kubernetes deployment

The `k8s/` directory contains manifests for deploying to a cluster with an NFS server.

### 1. Configure the NFS PersistentVolume

Edit `k8s/pv-nfs.yaml` and set your NFS server address and export path:

```yaml
nfs:
  server: 192.168.1.100   # your NFS server
  path: /export/data       # your export path
```

### 2. Create Azure credentials

Create a secret with your Azure Storage credentials:

```bash
kubectl create secret generic azure-credentials \
  --from-literal=APP_AZURE_ACCOUNT_URL=https://<account>.blob.core.windows.net \
  --from-literal=APP_AZURE_ACCOUNT_KEY=<key>
```

Or configure managed identity and omit the secret entirely.

### 3. Update the deployment

Edit `k8s/deployment.yaml` to set `APP_AZURE_ACCOUNT_URL` to your storage account and update the OTel endpoint if you have a collector running in the cluster.

### 4. Deploy

```bash
make k3s-apply
```

This creates the PV, PVC, and Deployment. The pod mounts the NFS share at `/mnt/nfs` and uses an `emptyDir` volume (up to 100Gi) for local staging.

To tear down:

```bash
make k3s-delete
```

### Resource defaults

The deployment requests 250m CPU / 256Mi memory and limits to 2 CPU / 1Gi memory. Ephemeral storage is set to 110Gi to accommodate the local staging volume. Adjust these based on your file sizes and throughput needs.

## Observability

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, the service exports traces and metrics via OTLP HTTP.

Custom metrics:

| Metric | Type | Description |
|---|---|---|
| `files.processed` | Counter | Successful uploads |
| `files.failed` | Counter | Failed uploads |
| `upload.duration` | Histogram | Upload time per file (seconds) |
| `file.size` | Histogram | File sizes (bytes) |
| `queue.depth` | UpDownCounter | Current queue depth |

All log output is structured JSON with OpenTelemetry trace context (`trace_id`, `span_id`) injected into each line.

## Makefile reference

| Target | Description |
|---|---|
| `make install` | Install the project in editable mode with dev dependencies |
| `make run` | Run the app locally with hot reload |
| `make dev-up` | Start Azurite + OTel Collector for local dev |
| `make dev-down` | Stop dev containers |
| `make docker-build` | Build the app Docker image |
| `make docker-up` | Build and start the full stack |
| `make docker-down` | Stop the full stack |
| `make lint` | Run ruff linter |
| `make format` | Run ruff formatter |
| `make typecheck` | Run pyright type checker |
| `make k3s-apply` | Deploy Kubernetes manifests |
| `make k3s-delete` | Remove Kubernetes resources |
| `make clean` | Delete data/, caches, and build artifacts |
| `make watch-start` | Start a watch session |
| `make watch-stop` | Stop the watch session |
| `make gen-start` | Start the test file generator |
| `make gen-stop` | Stop the test file generator |
| `make test-status` | Show app and generator status |

## Project structure

```
app/
  main.py             FastAPI app, lifespan, routes
  config.py           Environment-based settings
  watcher.py          NFS polling loop
  worker.py           Upload worker pool
  azure_client.py     Azure Blob client lifecycle
  session.py          Session state management
  recovery.py         Startup recovery for interrupted uploads
  gc.py               Background cleanup of completed files
  telemetry.py        OpenTelemetry + structured logging setup
  models.py           Pydantic models and WorkItem dataclass
test-nfs/
  main.py             Test file generator FastAPI app
  Dockerfile          Container image for the generator
k8s/
  deployment.yaml     Kubernetes Deployment
  pv-nfs.yaml         NFS PersistentVolume
  pvc-nfs.yaml        NFS PersistentVolumeClaim
```
