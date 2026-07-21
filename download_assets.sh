#!/bin/bash

# Script para descargar fuentes básicas para el motor de mapas
# Basado en las fuentes de MapLibre/OpenMapTiles

# Cambiar al directorio del script
cd "$(dirname "$0")"

mkdir -p "fonts/Open Sans Regular"
mkdir -p "fonts/Open Sans Bold"

echo "Descargando glifos de ejemplo (Open Sans)..."

curl -L https://fonts.openmaptiles.org/Open%20Sans%20Regular/0-255.pbf -o "fonts/Open Sans Regular/0-255.pbf"
curl -L https://fonts.openmaptiles.org/Open%20Sans%20Bold/0-255.pbf -o "fonts/Open Sans Bold/0-255.pbf"

echo "✅ Fuentes básicas descargadas en /fonts"
