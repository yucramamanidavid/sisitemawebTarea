# ====== Build stage (instalación limpia de deps) ======
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependencias del sistema (compilar algunos wheels si hace falta)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia solo requirements primero (mejor cache)
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip wheel --no-cache-dir --no-deps -r requirements.txt -w /wheels

# ====== Runtime stage (ligero) ======
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GUNICORN_CMD_ARGS="--bind 0.0.0.0:5000 --workers 3 --timeout 60 --access-logfile - --error-logfile -" \
    UPLOAD_DIR=/app/uploads

# Dependencias mínimas para runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Crea usuario no-root
RUN useradd -ms /bin/bash appuser

WORKDIR /app

# Instala deps desde el stage builder
COPY --from=builder /wheels /wheels
COPY --from=builder /app/requirements.txt /app/requirements.txt
RUN pip install --no-cache /wheels/* && rm -rf /wheels

# Copia el código
COPY . /app

# Crea carpeta de uploads y ajusta permisos
RUN mkdir -p ${UPLOAD_DIR} && chown -R appuser:appuser /app

# Cambia a usuario no-root
USER appuser

EXPOSE 5000

# Usa gunicorn llamando a tu app Flask (módulo:app_variable)
# Si tu archivo principal es app.py y la instancia Flask es 'app', entonces:
CMD ["gunicorn", "app:app"]
