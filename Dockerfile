# Stage 1: build frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/ ./
RUN NODE_ENV=development npm ci
RUN npm run build

# Stage 2: backend + serve frontend
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium --with-deps

COPY . .
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

EXPOSE 8005
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
