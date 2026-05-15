#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  NeuralJira — import_data.sh v2
#  Importación paralela con hash-check y auto-indexación
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

DB_CONN="PG:host=localhost port=5433 user=mapengine dbname=mapdb password=mapengine123"
SRC="${1:-data/mapa_guajira.gpkg}"
MAX_PARALLEL="${2:-4}"          # ajusta según cores de la VPS
HASH_FILE="data/.import_hashes"
EXCLUDED="layer_styles"

# ── Colores ──────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR]${NC}   $*"; }

if [ ! -f "$SRC" ]; then
    error "Archivo no encontrado: $SRC"
    exit 1
fi

# ── Hash del archivo fuente ───────────────────────────────────────
FILE_HASH=$(md5sum "$SRC" | cut -d' ' -f1)
info "Archivo: $SRC (MD5: $FILE_HASH)"

touch "$HASH_FILE"

# ── Listar capas (excluyendo layer_styles) ────────────────────────
LAYERS=$(ogrinfo -so "$SRC" | grep -E '^[0-9]+:' | \
    cut -d':' -f2 | cut -d'(' -f1 | \
    sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | \
    grep -v "$EXCLUDED" || true)

TOTAL=$(echo "$LAYERS" | grep -c '\S' || echo 0)
info "Capas encontradas: $TOTAL | Paralelas: $MAX_PARALLEL"

# ── Función de importación de una capa ───────────────────────────
import_layer() {
    local layer="$1"
    local src="$2"
    local db_conn="$3"
    local hash_file="$4"
    local file_hash="$5"

    local hash_key="${src}::${layer}"
    local stored_hash
    stored_hash=$(grep -F "$hash_key" "$hash_file" 2>/dev/null | cut -d'=' -f2 || echo "")

    if [ "$stored_hash" = "$file_hash" ]; then
        echo -e "${YELLOW}[SKIP]${NC}  $layer — sin cambios"
        return 0
    fi

    echo -e "${CYAN}[PROC]${NC}  Importando: $layer"
    local start_time
    start_time=$(date +%s)

    if ogr2ogr -f PostgreSQL "$db_conn" "$src" "$layer" \
        --config PG_USE_COPY YES \
        -nln "${layer,,}" \
        -nlt PROMOTE_TO_MULTI \
        -lco GEOMETRY_NAME=geometry \
        -lco OVERWRITE=YES \
        -t_srs EPSG:4326 \
        -makevalid \
        -skipfailures \
        2>&1 | grep -v "^$"; then

        local end_time duration
        end_time=$(date +%s)
        duration=$((end_time - start_time))

        # Guardar hash de la capa importada exitosamente
        # Eliminar entrada anterior y agregar nueva (thread-safe con flock)
        (
            flock -x 200
            grep -vF "$hash_key" "$hash_file" > "${hash_file}.tmp" || true
            echo "${hash_key}=${file_hash}" >> "${hash_file}.tmp"
            mv "${hash_file}.tmp" "$hash_file"
        ) 200>"${hash_file}.lock"

        echo -e "${GREEN}[DONE]${NC}  $layer (${duration}s)"
        return 0
    else
        echo -e "${RED}[FAIL]${NC}  $layer"
        return 1
    fi
}

export -f import_layer

# ── Ejecutar en paralelo ──────────────────────────────────────────
FAILED=0
echo "$LAYERS" | xargs -P "$MAX_PARALLEL" -I {} bash -c \
    'import_layer "$@"' _ {} "$SRC" "$DB_CONN" "$HASH_FILE" "$FILE_HASH" \
    || FAILED=$?

# ── Auto-indexación post-importación ─────────────────────────────
info "Creando índices GIST y actualizando estadísticas..."

ALL_TABLES=$(docker exec map-db-1 psql -U mapengine -d mapdb -t -c \
    "SELECT f_table_name FROM geometry_columns WHERE f_table_schema='public';" 2>/dev/null || \
    psql "$DB_CONN" -t -c \
    "SELECT f_table_name FROM geometry_columns WHERE f_table_schema='public';" 2>/dev/null || echo "")

while IFS= read -r table; do
    table=$(echo "$table" | xargs)
    [ -z "$table" ] && continue
    info "  Indexando: $table"
    docker exec map-db-1 psql -U mapengine -d mapdb -c \
        "CREATE INDEX IF NOT EXISTS idx_${table}_geom ON \"$table\" USING GIST (geometry);
         ANALYZE \"$table\";" 2>/dev/null || true
done <<< "$ALL_TABLES"

# ── Resultado ─────────────────────────────────────────────────────
echo ""
if [ "$FAILED" -eq 0 ]; then
    success "Importación completada exitosamente."
else
    warn "Importación completada con $FAILED error(es). Revisa los logs."
fi
