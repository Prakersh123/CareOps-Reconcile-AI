# CareOps Reconcile AI: API + web UI in one image. No GPU, no runtime installs.
#
#   docker build -t careops .
#   docker run --rm -p 8000:8000 --env-file .env \
#     -e ZETARIS_BASE_URL=http://host.docker.internal:3000 \
#     --add-host=host.docker.internal:host-gateway careops
#
# Secrets come from the environment at run time; none are baked into the image.

# ---- web UI build -----------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---- app --------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOST=0.0.0.0 \
    PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY data/ data/
COPY run.py .
COPY --from=ui /ui/dist ui/dist

RUN mkdir -p /app/logs /app/reports /app/.world

EXPOSE 8000

CMD ["python", "run.py"]
