#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  NeuralJira — optimize_db.sh v2
#  Optimización dinámica de TODAS las tablas geoespaciales
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

CONTAINER="${1:-map-db}"
DB_USER="${2:-mapengine}"
DB_NAME="${3:-mapdb}"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }

psql_exec() {
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "$1"
}

info "Obteniendo lista de tablas geoespaciales..."
TABLES=$(docker exec "$CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -c \
    "SELECT f_table_name FROM geometry_columns WHERE f_table_schema='public' ORDER BY f_table_name;")

while IFS= read -r table; do
    table=$(echo "$table" | xargs)
    [ -z "$table" ] && continue

    info "Optimizando: $table"

    # 1. Índice GIST (si no existe)
    psql_exec "CREATE INDEX IF NOT EXISTS idx_${table}_geom ON \"$table\" USING GIST (geometry);"

    # 2. Clusterizar por índice espacial (mejora lectura de tiles)
    psql_exec "CLUSTER \"$table\" USING idx_${table}_geom;" 2>/dev/null || \
        info "  CLUSTER omitido (tabla vacía o bloqueada)"

    # 3. Actualizar estadísticas del planificador
    psql_exec "ANALYZE \"$table\";"

    # 4. Autovacuum agresivo para tablas de escritura frecuente
    psql_exec "ALTER TABLE \"$table\" SET (
        autovacuum_vacuum_scale_factor = 0.01,
        autovacuum_analyze_scale_factor = 0.005
    );"

    success "$table optimizada"
done <<< "$TABLES"

# ── Checkpoint forzado ────────────────────────────────────────────
info "Forzando checkpoint..."
psql_exec "CHECKPOINT;"

success "Optimización completada. Martin debería volar 🚀"
