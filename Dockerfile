# syntax=docker/dockerfile:1.7

# --- Etapa 1: dependencias ------------------------------------------------
# Se instalan en una venv aislada y se copia entera a la etapa final. Así ni
# el toolchain de compilación ni la caché de pip viajan a la imagen desplegada.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# --- Etapa 2: runtime -----------------------------------------------------
FROM python:3.12-slim AS runtime

# Sin .pyc y con salida sin buffer: `docker logs` legible y menos I/O.
#
# Las rutas van por entorno y no en config.yaml: el contenedor no comparte el
# layout del repo. `ELA_PROJECT_ROOT=/app` ancla las rutas relativas que quedan
# (patterns_dir, rules_path) bajo /app.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/ela \
    ELA_PROJECT_ROOT=/app \
    ELA_LOG_DIR=/logs \
    ELA_DB_PATH=/data/app.db \
    ELA_EXPORT_DIR=/exports \
    LOGLENS_TRACES_DIR=/traces \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

COPY --from=builder /opt/venv /opt/venv

# Usuario no-root. Los directorios de los volúmenes se crean aquí para que
# Docker los inicialice con este dueño (los named volumes heredan permisos).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin ela \
    && mkdir -p /app /logs /data /exports /traces \
    && chown -R ela:ela /app /logs /data /exports /traces

WORKDIR /app

# Sólo lo necesario para ejecutar: el código y sus patrones/reglas viven bajo
# app/; config.yaml aporta los defaults que las env de arriba sobreescriben.
COPY --chown=ela:ela app/ ./app/
COPY --chown=ela:ela config.yaml ./

USER ela

# 8501 dashboard · 8000 MCP streamable-http
EXPOSE 8501 8000

# Por defecto, una pasada de ingesta. Compose sobreescribe el comando de cada
# servicio (`--watch` para analyzer, `streamlit run` para dashboard).
CMD ["python", "-m", "app.ingest", "--once"]
