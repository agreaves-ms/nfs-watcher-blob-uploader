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
                ext.strip().lower()
                if ext.strip().startswith(".")
                else f".{ext.strip().lower()}"
                for ext in v.split(",")
                if ext.strip()
            )
        return v
