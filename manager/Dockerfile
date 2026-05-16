FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias de sistema para GIS (GDAL es crítico)
RUN apt-get update && apt-get install -y \
    g++ \
    libgdal-dev \
    gdal-bin \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Variables de entorno para que fiona encuentre GDAL
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
