FROM python:3.12-slim

WORKDIR /app

# Dependencias del sistema (solo si hicieran falta certificados extras)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código
COPY mcp_server.py .

# Puerto que expone el servidor
EXPOSE 8080

# Healthcheck para que EasyPanel sepa si el contenedor está vivo
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=3)" || exit 1

CMD ["python", "mcp_server.py"]
