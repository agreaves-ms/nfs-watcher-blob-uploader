# Stage 1: dependencies
FROM python:3.12-slim-bookworm AS deps
WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --target=/deps .

# Stage 2: runtime
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=deps /deps /usr/local/lib/python3.12/site-packages
COPY --from=deps /deps/bin /usr/local/bin
COPY app/ ./app/
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
