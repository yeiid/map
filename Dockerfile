FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for GDAL/PostGIS tools
RUN apt-get update && apt-get install -y \
    libgdal-dev \
    gdal-bin \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from the manager folder
COPY manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Move manager contents to root to maintain path compatibility (fonts, data, styles)
RUN cp -r manager/* . && rm -rf manager

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
