# 🗺️ Plan de Mejoras — NeuralJira Map Engine
> Análisis de producción y hoja de ruta de optimización
> *Generado: 2026-05-14*

---

## 🔴 Diagnóstico: Problemas Críticos Actuales

### 1. El cuello de botella principal — `process_gdf()` carga TODO en RAM

```python
# ❌ ACTUAL: Lee todo el archivo de 3 GB en memoria de una vez
gdf = gpd.read_file(filepath, layer=layer, engine="pyogrio")
df.to_postgis(table_name, engine, if_exists='replace', index=False)
```

**Impacto:** Para un GPKG de 3 GB con capas de polígonos catastrales:
- GeoPandas carga el GeoDataFrame completo → **≈8-12 GB RAM** de uso pico (geometrías descomprimidas)
- `to_postgis` hace **un INSERT por fila** (sin `method='multi'` ni `COPY`)
- Bloquea el **hilo principal de FastAPI** durante minutos → el servidor queda congelado
- No hay progreso, no hay timeout, no hay rollback si falla a mitad

---

### 2. `import_data.sh` — Procesamiento secuencial capa por capa

```bash
# ❌ ACTUAL: ogr2ogr corre de forma secuencial
while read -r layer; do
    ogr2ogr -f PostgreSQL "$DB_CONN" "$SRC" "$layer" \
        --config PG_USE_COPY YES ...
done
```

**Impacto:**
- Con ~15 capas en el GPKG y polígonos pesados, el proceso toma **20-60 minutos** secuencialmente
- No hay paralelismo; 1 sola capa usa 1 core → el resto de la VPS está idle
- Sin control de cuáles capas ya existen y son idénticas (re-importa TODO)

---

### 3. `list_layers` — N+1 queries sin caché

```python
# ❌ ACTUAL: Una query COUNT(*) por cada capa
for r in rows:
    count = conn.execute(text(f"SELECT count(*) FROM {r[0]}")).scalar()
```

**Impacto:** Con 15 capas → 16 queries síncronas en cada llamada. Con polígonos de millones de filas, `COUNT(*)` en PostGIS sin estadísticas puede tardar segundos.

---

### 4. `admin_stats` — Abre 2 conexiones separadas + N+1

```python
# ❌ ACTUAL
engine = create_engine(SYNC_DB_URL)  # nueva engine en cada request
with engine.connect() as conn: ...   # conexión 1
with engine.connect() as conn: ...   # conexión 2 (innecesaria)
```

**Impacto:** Se crean engines SQLAlchemy (con pool) en **cada petición HTTP**. Sin un pool compartido global, cada request crea y destruye conexiones → overhead de TCP + autenticación PostgreSQL en cada llamada.

---

### 5. `track_usage()` — Lectura/escritura de JSON en cada request

```python
# ❌ ACTUAL: Lee y escribe JSON en disco en CADA petición
def track_usage(...):
    usage = load_usage()    # read from disk
    ...
    save_usage(usage)       # write to disk
```

**Impacto:** Bajo concurrencia, múltiples requests simultáneos generan **race conditions** y corrupción del JSON. Cada request hace 2 I/O de disco síncronos.

---

### 6. `get_style()` — Regenera el estilo completo en cada request

```python
# ❌ ACTUAL: Queries a geometry_columns + construcción del dict en cada request
@app.get("/api/v1/style.json")
async def get_style(request: Request):
    engine = create_engine(SYNC_DB_URL)  # Nueva engine
    with engine.connect() as conn:
        rows = conn.execute(...)  # Queries DB
```

**Impacto:** Cada cliente que carga el mapa hace una query a `geometry_columns`. Con 50 usuarios concurrentes → 50 queries simultáneas para datos que rara vez cambian.

---

### 7. `optimize_db.sh` — Hardcoded para capas específicas

```bash
tables=("u_construccion" "r_construccion" "u_terreno" ...)
```

**Impacto:** Si se importan nuevas capas, el script no las optimiza. Los índices GIST no se crean automáticamente después de cada importación.

---

## 🟢 Arquitectura Propuesta — Stack Optimizado

```
┌─────────────────────────────────────────────────────────────┐
│                     CLIENTE / FRONTEND                       │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTP/HTTPS
┌────────────────────────▼────────────────────────────────────┐
│           FastAPI (manager) — Pool de Conexiones             │
│   ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│   │  Auth + JWT  │  │  Style Cache │  │  Job Queue API  │  │
│   └──────────────┘  └──────────────┘  └────────┬────────┘  │
└─────────────────────────────────────────────────┼───────────┘
                                                  │ enqueue
┌─────────────────────────────────────────────────▼───────────┐
│              ARQ / Celery Worker (Background)                │
│   ┌─────────────────────────────────────────────────────┐   │
│   │  Ingesta incremental por chunks (ogr2ogr paralelo)  │   │
│   │  Auto-indexación GIST post-importación              │   │
│   │  VACUUM + ANALYZE automático                        │   │
│   └─────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                  PostGIS (optimizado)                        │
│   ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│   │  pg_config   │  │  Índices     │  │  pg_partman     │  │
│   │  optimizado  │  │  GIST auto   │  │  (partición)    │  │
│   └──────────────┘  └──────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## 📋 Plan de Implementación — Fases

### FASE 1 — Correcciones Inmediatas (sin cambiar arquitectura)

#### 1.1 Engine SQLAlchemy Global con Pool

```python
# ✅ NUEVO: Engine compartida con pool de conexiones
from sqlalchemy import create_engine

engine = create_engine(
    SYNC_DB_URL,
    pool_size=10,          # conexiones persistentes
    max_overflow=20,       # conexiones extra bajo carga
    pool_pre_ping=True,    # valida conexiones antes de usar
    pool_recycle=3600,     # recicla conexiones cada hora
)
```

#### 1.2 `list_layers` — COUNT estimado via estadísticas PG

```python
# ✅ NUEVO: Usa pg_stat_user_tables (instantáneo, sin full scan)
rows = conn.execute(text("""
    SELECT 
        gc.f_table_name, gc.type, gc.srid,
        COALESCE(pg_stat.n_live_tup, 0) as features,
        pg_size_pretty(pg_total_relation_size(gc.f_table_name::regclass)) as size
    FROM geometry_columns gc
    LEFT JOIN pg_stat_user_tables pg_stat 
        ON pg_stat.relname = gc.f_table_name
    WHERE gc.f_table_schema = 'public'
"""))
```

#### 1.3 `get_style` — Caché en memoria con TTL

```python
# ✅ NUEVO: Caché de 5 minutos, se invalida al importar datos nuevos
import time
_style_cache = {"data": None, "ts": 0}
STYLE_CACHE_TTL = 300  # 5 minutos

async def get_style(request: Request):
    now = time.time()
    if _style_cache["data"] and (now - _style_cache["ts"]) < STYLE_CACHE_TTL:
        return _style_cache["data"]
    # ... generar style ...
    _style_cache.update({"data": style, "ts": now})
    return style
```

#### 1.4 `track_usage` — Usar Redis o base en memoria

Reemplazar lectura/escritura de JSON por un dict en memoria protegido con `asyncio.Lock()` y flush periódico al disco (cada 60 segundos via background task).

#### 1.5 `process_gdf` — Ingesta por chunks con COPY

```python
# ✅ NUEVO: Carga por lotes de 5000 filas, usa COPY
CHUNK_SIZE = 5000

def process_gdf_chunked(filepath, layer_name, engine):
    with fiona.open(filepath, layer=layer_name) as src:
        chunk = []
        for i, feature in enumerate(src):
            chunk.append(feature)
            if len(chunk) >= CHUNK_SIZE:
                _write_chunk(chunk, layer_name, engine, first=(i < CHUNK_SIZE))
                chunk = []
        if chunk:
            _write_chunk(chunk, layer_name, engine, first=False)
```

---

### FASE 2 — Importación Incremental + Background Jobs

#### 2.1 Hash de capas para detectar cambios

```python
# Guardar MD5/SHA256 del GPKG + nombre de capa
# Si el hash no cambió → skip importación
import hashlib

def get_file_hash(filepath: str) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```

#### 2.2 API de Jobs Asíncronos

```python
# POST /api/v1/admin/import/async → retorna job_id
# GET  /api/v1/admin/jobs/{job_id} → retorna status + progreso %

@app.post("/api/v1/admin/import/async")
async def import_async(background_tasks: BackgroundTasks, ...):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "pending", "progress": 0}
    background_tasks.add_task(run_import, job_id, filepath)
    return {"job_id": job_id}

@app.get("/api/v1/admin/jobs/{job_id}")
async def get_job(job_id: str):
    return jobs.get(job_id, {"status": "not_found"})
```

#### 2.3 Importación paralela con ogr2ogr (múltiples capas simultáneas)

```bash
# ✅ NUEVO import_data.sh con paralelismo
MAX_PARALLEL=4  # ajusta según cores de la VPS

export -f import_layer
echo "$layers" | xargs -P $MAX_PARALLEL -I {} bash -c 'import_layer "$@"' _ {}
```

---

### FASE 3 — PostgreSQL Tuning para la VPS

#### 3.1 `postgresql.conf` optimizado

```ini
# Para VPS con 4GB RAM — ajustar según recursos reales
shared_buffers = 1GB              # 25% del RAM
effective_cache_size = 3GB        # 75% del RAM
work_mem = 64MB                   # para operaciones de geometría
maintenance_work_mem = 256MB      # para CREATE INDEX
max_parallel_workers_per_gather = 2
enable_partitionwise_aggregate = on

# WAL optimización para escrituras masivas
wal_buffers = 64MB
checkpoint_completion_target = 0.9
min_wal_size = 512MB
max_wal_size = 2GB
```

#### 3.2 Auto-indexación post-importación

```sql
-- Ejecutar automáticamente después de cada importación:
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_{table}_geom 
    ON {table} USING GIST (geometry);
    
-- Para capas con muchas consultas por atributo (ej: número predial):
CREATE INDEX IF NOT EXISTS idx_{table}_predial 
    ON {table} (numero_predial);

-- Clusterizar + analizar
CLUSTER {table} USING idx_{table}_geom;
ANALYZE {table};
```

#### 3.3 Vacuum automático agresivo post-import

```sql
ALTER TABLE {table} SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_analyze_scale_factor = 0.005
);
```

---

### FASE 4 — Arquitectura Delta (evitar re-procesar 3GB)

#### Concepto: "Importación Diferencial"

En vez de reemplazar toda la DB con cada actualización:

1. **Versionado de datos**: Cada GPKG importado se registra con su hash y fecha
2. **Modo UPSERT**: Detectar filas nuevas/modificadas por campo clave (ej: `codigo_predial`)
3. **Tablas versionadas**: `u_construccion_v1`, `u_construccion_v2` con vista activa `u_construccion`
4. **Swap atómico**: 

```sql
-- Importar en tabla staging, luego swap atómico:
BEGIN;
ALTER TABLE u_construccion RENAME TO u_construccion_old;
ALTER TABLE u_construccion_staging RENAME TO u_construccion;
DROP TABLE u_construccion_old;
COMMIT;
```

Esto garantiza **zero downtime** durante actualizaciones y evita procesar 3GB si solo el 5% de los datos cambió.

---

## 📊 Mejoras de Rendimiento Estimadas

| Métrica | Actual | Optimizado | Ganancia |
|--------|--------|-----------|---------|
| Tiempo ingesta 3GB GPKG | 30-60 min | 8-15 min | **4x más rápido** |
| RAM pico durante importación | 8-12 GB | 1-2 GB | **6-8x menos** |
| Latencia `GET /api/v1/layers` | 2-5s | < 50ms | **40-100x más rápido** |
| Latencia `GET /api/v1/style.json` | 200-500ms | < 10ms | **50x más rápido** |
| Tiempo re-importación si datos no cambian | 30-60 min | 0s (skip) | **∞** |
| Disponibilidad durante importación | 0% (bloqueado) | 100% (async) | **Crítico** |

---

## 🚀 Archivos a Crear/Modificar

| Archivo | Acción | Prioridad |
|---------|--------|----------|
| `manager/main.py` | Refactorizar con engine global, caché de estilos, jobs async | 🔴 Alta |
| `import_data.sh` | Añadir paralelismo `xargs -P`, hash checking, auto-index | 🔴 Alta |
| `docker-compose.yml` | Añadir Redis (para jobs/caché), tuning PostGIS env vars | 🟡 Media |
| `optimize_db.sh` | Hacer dinámico (todas las tablas), auto-ejecutar post-import | 🟡 Media |
| `manager/ingest.py` | Nuevo módulo de ingesta incremental por chunks | 🟡 Media |
| `init_postgres.sql` | Script de configuración PostgreSQL optimizado | 🟢 Baja |

---

## ⚡ Quick Wins — Implementar Ahora (< 30 min cada uno)

1. **Pool global** → mover `create_engine()` fuera de los endpoints
2. **COUNT aproximado** → usar `pg_stat_user_tables` en `list_layers`
3. **Caché style.json** → dict en memoria con TTL de 5 minutos
4. **Proceso async** → `background_tasks.add_task()` para importaciones

> [!IMPORTANT]
> La mejora más crítica es la #4: actualmente una importación de 3GB **bloquea FastAPI completamente**, dejando el servicio de tiles y el frontend sin respuesta durante la actualización.

> [!TIP]
> Con el pool de conexiones global y el COUNT aproximado, la latencia del endpoint `/api/v1/layers` debería caer de segundos a milisegundos sin ningún cambio en la infraestructura.

> [!WARNING]
> El `track_usage()` actual tiene una **race condition** bajo concurrencia: dos requests simultáneos pueden leer el mismo JSON, hacer sus cambios, y el primero que guarda pierde los cambios del otro. Necesita un lock o migración a Redis/SQLite.
