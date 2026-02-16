import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, field_validator

# --- Work queue item ---

SESSION_NAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Unit of work representing a file to be uploaded."""

    source_path: Path
    session_name: str
    date_prefix: str
    filename: str
    from_recovery: bool


# --- API request/response ---


class WatchStartRequest(BaseModel):
    session_name: str | None = None

    @field_validator("session_name")
    @classmethod
    def validate_session_name(cls, v: str | None) -> str | None:
        if v is not None and not SESSION_NAME_RE.match(v):
            raise ValueError("session_name must match [a-zA-Z0-9_.-]")
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
