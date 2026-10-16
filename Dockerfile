# -- stage 1: build the React app (web/ -> web/dist). `npm run build` type-checks first. --------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# -- stage 2: the Python service, with the built app dropped in where the server looks for it ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
COPY --from=web /web/dist ./web/dist

# Run as an unprivileged user; /app/data is the only writable path.
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app:app /app/data
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# --ws-max-size caps inbound WebSocket frames (clients only ever send a tiny auth frame).
CMD ["uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "65536"]
