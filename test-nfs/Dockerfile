FROM python:3.12-slim-bookworm
WORKDIR /app
RUN pip install --no-cache-dir fastapi "uvicorn[standard]"
COPY main.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
