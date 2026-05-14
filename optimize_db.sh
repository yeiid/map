#!/bin/bash
# 🚀 NeuralJira DB Optimizer
# Para manejar GPKGs de 3GB y millones de registros con fluidez.

DB_CONN="postgresql://mapengine:mapengine123@localhost:5432/mapdb"

echo "Optimizando tablas para alto rendimiento..."

tables=("u_construccion" "r_construccion" "u_terreno" "u_nomenclatura_vial" "dterritorialesmunpio")

for table in "${tables[@]}"; do
    echo "Processing $table..."
    
    # 1. Crear índice espacial si no existe
    sudo docker exec map-db-1 psql -U mapengine -d mapdb -c \
        "CREATE INDEX IF NOT EXISTS idx_${table}_geom ON $table USING GIST (geometry);"
    
    # 2. Clusterizar (Agrupar físicamente los datos por ubicación)
    # Esto acelera enormemente la lectura de tiles
    echo "Clustering $table by geometry..."
    sudo docker exec map-db-1 psql -U mapengine -d mapdb -c \
        "CLUSTER $table USING idx_${table}_geom;"
    
    # 3. Analizar para el planificador de consultas
    sudo docker exec map-db-1 psql -U mapengine -d mapdb -c \
        "ANALYZE $table;"
done

echo "✅ Optimización completada. Martin ahora debería volar."
