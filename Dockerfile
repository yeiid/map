FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema para GDAL/PostGIS
RUN apt-get update && apt-get install -y \
    libgdal-dev \
    gdal-bin \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copiar solo requerimientos primero para aprovechar el caché de Docker
COPY manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código del manager directamente al WORKDIR
# Esto evita tener que mover archivos después y acelera la build
COPY manager/ .

# Exponer el puerto
EXPOSE 8000

# Comando de inicio
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
