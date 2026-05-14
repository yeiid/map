#!/bin/bash
DB_CONN="PG:host=localhost user=mapengine dbname=mapdb password=mapengine123"
SRC="data/mapa_guajira.gpkg"

echo "Importando capas desde $SRC a PostGIS..."

# Obtener lista de capas reales (saltando layer_styles)
layers=$(ogrinfo -so "$SRC" | grep ":" | cut -d":" -f2 | cut -d"(" -f1 | sed 's/^[ \t]*//;s/[ \t]*$//' | grep -v "layer_styles")

for layer in $layers; do
    echo "Procesando capa: $layer"
    ogr2ogr -f PostgreSQL "$DB_CONN" "$SRC" "$layer" \
        -nln "${layer,,}" \
        -nlt PROMOTE_TO_MULTI \
        -lco GEOMETRY_NAME=geometry \
        -lco FID=fid \
        -lco OVERWRITE=YES \
        -t_srs EPSG:4326 \
        -makevalid \
        -skipfailures
done

echo "✅ Importación finalizada."
