#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  NeuralJira — import_data.sh v3 (Smart Sync)
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# Colores
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }

# 1. Obtener Token de Admin (usando las credenciales por defecto)
info "Autenticando con el Manager..."
TOKEN=$(curl -s -X POST "http://localhost/api/v1/auth/login" \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "username=admin&password=admin123" | jq -r '.access_token')

if [ "$TOKEN" == "null" ] || [ -z "$TOKEN" ]; then
    echo "Error: No se pudo obtener el token de autenticación. ¿Está el manager corriendo?"
    exit 1
fi

# 2. Disparar el Scan de la carpeta Data
info "Iniciando sincronización incremental de /data..."
FORCE=${1:-false}

RESPONSE=$(curl -s -X POST "http://localhost/api/v1/admin/import/scan?force=$FORCE" \
     -H "Authorization: Bearer $TOKEN")

JOB_IDS=$(echo "$RESPONSE" | jq -r '.jobs[].job_id' 2>/dev/null || echo "")

if [ -z "$JOB_IDS" ]; then
    success "Todo está al día. No se requiere importación."
    exit 0
fi

info "Sincronización iniciada. Puedes seguir el progreso en: http://localhost/admin"
success "Se han lanzado $(echo "$JOB_IDS" | wc -l) tareas de importación."
