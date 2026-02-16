"""OpenTelemetry setup: traces, metrics, and structured JSON logging."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

if TYPE_CHECKING:
    from fastapi import FastAPI


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter with OTel trace context injection."""

    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span()
        ctx = span.get_span_context()

        log_record: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": f"{ctx.trace_id:032x}" if ctx.trace_id else "",
            "span_id": f"{ctx.span_id:016x}" if ctx.span_id else "",
            "trace_flags": ctx.trace_flags,
        }

        # Merge structured extra fields
        for key in (
            "file_name",
            "session_name",
            "date_prefix",
            "blob_name",
            "size_bytes",
            "duration_s",
        ):
            val = getattr(record, key, None)
            if val is not None:
                log_record[key] = val

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            log_record["exception"] = record.exc_text

        return json.dumps(log_record, default=str)


def setup_telemetry(app: FastAPI) -> tuple[TracerProvider, MeterProvider]:
    """Initialize OTel providers and structured logging. Call early in lifespan."""
    service_name = os.environ.get("OTEL_SERVICE_NAME", "nfs-watcher-uploader")
    resource = Resource.create({"service.name": service_name})

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # Metrics
    readers = []
    if endpoint:
        readers.append(PeriodicExportingMetricReader(OTLPMetricExporter()))
    meter_provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(meter_provider)

    # FastAPI auto-instrumentation
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")

    # Structured JSON logging
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Quiet noisy loggers
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return tracer_provider, meter_provider


# Custom metrics — importable by other modules
_meter = metrics.get_meter("nfs-watcher-uploader")

files_processed = _meter.create_counter(
    "files.processed", description="Files uploaded successfully"
)
files_failed = _meter.create_counter(
    "files.failed", description="Files that failed processing"
)
upload_duration = _meter.create_histogram(
    "upload.duration", unit="s", description="Upload duration"
)
file_size_hist = _meter.create_histogram(
    "file.size", unit="By", description="Uploaded file size"
)
queue_depth = _meter.create_up_down_counter(
    "queue.depth", description="Current work queue depth"
)
