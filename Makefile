.PHONY: install run dev-up dev-down docker-up docker-down docker-build \
        lint format typecheck clean k3s-apply k3s-delete

# --- Setup ---

install:
	pip install -e ".[dev]"

# --- Local development (app on host, deps in Docker) ---

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

dev-up:
	docker compose -f docker-compose.dev.yaml up -d

dev-down:
	docker compose -f docker-compose.dev.yaml down

# --- Docker Compose full stack ---

docker-build:
	docker build -t nfs-watcher-uploader:latest .

docker-up: docker-build
	docker compose up -d

docker-down:
	docker compose down

# --- Code quality ---

lint:
	ruff check app/

format:
	ruff format app/

typecheck:
	pyright app/

# --- k3s ---

k3s-apply:
	kubectl apply -f k8s/

k3s-delete:
	kubectl delete -f k8s/

# --- Cleanup ---

clean:
	rm -rf data/ __pycache__ .ruff_cache .pyright
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
