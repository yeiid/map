# NeuralJira Map Engine

Sistema independiente de mapas (Map-as-a-Service) diseñado para servir datos geoespaciales catastrales con alto rendimiento. Construido sobre PostGIS, Martin y FastAPI, desplegable en Dokploy o cualquier entorno Docker.

## Arquitectura

```
Cliente (MapLibre GL JS)
        │
        │ HTTP/HTTPS
        ▼
┌───────────────────┐
│   Nginx (Proxy)   │  Puerto 80 — Caché de tiles, CORS, Gzip
└───────┬───────────┘
        │
    ┌───┴───┐
    │       │
    ▼       ▼
┌──────┐ ┌──────┐
│Manager│ │Martin│
│ :8000 │ │:3000 │
└──┬────┘ └──┬───┘
   │         │
   └────┬────┘
        ▼
┌──────────────┐
│   PostGIS    │  PostgreSQL 15 + PostGIS 3.4
│  :5432       │
└──────────────┘
```

### Componentes

| Servicio | Imagen | Rol |
|----------|--------|-----|
| **db** | `postgis/postgis:15-3.4` | Base de datos espacial con índices GIST |
| **tiles** | `maplibre/martin:latest` | Servidor de Vector Tiles MVT que lee directo de PostGIS |
| **manager** | `python:3.11-slim` | API FastAPI: estilos dinámicos, autenticación, ingesta de datos, jobs asíncronos |
| **gateway** | `nginx:alpine` | Reverse proxy: compresión gzip, CORS, caché de tiles |

### Stack técnico

- **Backend**: Python 3.11, FastAPI, SQLAlchemy (pool de conexiones), asyncpg
- **Tile Server**: Martin v0.x — renderiza MVT directamente desde PostGIS
- **Base de datos**: PostgreSQL 15 + PostGIS 3.4
- **Proxy**: Nginx con compresión gzip para tiles protobuf
- **Frontend**: MapLibre GL JS v3

## Requisitos

- Docker + Docker Compose
- 4 GB RAM mínimo (recomendado 8 GB para GPKG > 3 GB)
- Archivo de datos geoespaciales (GeoPackage, GeoJSON)
- Fuentes PBF en `fonts/` (opcional, usa Open Sans por defecto)

## Instalación rápida

```bash
# 1. Clonar o copiar archivos
# 2. Configurar entorno (opcional)
cp .env .env.local  # editar si es necesario

# 3. Iniciar stack
docker compose up -d

# 4. Importar datos
# Opción A: Subir por el panel admin
#   Abrir http://localhost/admin  →  login: admin / admin123  →  Escanear Carpeta Data
# Opción B: Por terminal
curl -X POST http://localhost/api/v1/admin/import/scan?force=true

# 5. Ver mapa
#   Vista pública:  http://localhost/preview
#   Panel admin:    http://localhost/admin
```

## Configuración

### Variables de entorno (`.env`)

| Variable | Default | Descripción |
|----------|---------|-------------|
| `POSTGRES_USER` | `mapengine` | Usuario de base de datos |
| `POSTGRES_PASSWORD` | `mapengine123` | Contraseña de base de datos |
| `POSTGRES_DB` | `mapdb` | Nombre de base de datos |
| `DB_PORT` | `5433` | Puerto expuesto de PostgreSQL |
| `PUBLIC_MARTIN_URL` | `http://localhost/tiles/` | URL pública del tile server |
| `SECRET_KEY` | `...` | Clave JWT para autenticación |
| `PG_SHARED_BUFFERS` | `256MB` | Buffer compartido de PostgreSQL |
| `PG_EFFECTIVE_CACHE_SIZE` | `768MB` | Tamaño de caché efectivo |
| `PG_WORK_MEM` | `16MB` | Memoria por operación |
| `PG_MAINTENANCE_WORK_MEM` | `64MB` | Memoria para mantenimiento |

### Ajustes de PostgreSQL

Para VPS con más RAM, ajustar en `.env`:

```env
# VPS de 4 GB RAM
PG_SHARED_BUFFERS=1GB
PG_EFFECTIVE_CACHE_SIZE=3GB
PG_WORK_MEM=64MB
PG_MAINTENANCE_WORK_MEM=256MB
```

## Importación de datos

### Desde el panel admin

1. Abrir `http://localhost/admin`
2. Login: `admin` / `admin123`
3. Arrastrar o seleccionar archivo GPKG/GeoJSON
4. Click **Importar** o **Escanear Carpeta Data**

El proceso corre en segundo plano — puedes ver el progreso en el panel.

### Desde terminal con `import_data.sh`

```bash
# Importación paralela (4 capas simultáneas)
./import_data.sh data/mapa_guajira.gpkg 4

# El script:
# 1. Limpia WAL del GPKG automáticamente
# 2. Detecta capas con ogrinfo
# 3. Importa en paralelo con ogr2ogr (PG_USE_COPY)
# 4. Omite capas sin cambios (hash MD5)
# 5. Crea índices GIST post-importación
```

### Desde QGIS

1. Abrir QGIS
2. Conectar a: `host=localhost port=5433 dbname=mapdb user=mapengine password=mapengine123`
3. Arrastrar capas a la base de datos
4. Martin las detecta automáticamente

## API endpoints

### Públicos

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/api/v1/config` | Configuración del tile server |
| `GET` | `/api/v1/style.json` | Estilo MapLibre GL dinámico (caché 5 min) |
| `GET` | `/api/v1/layers` | Lista de capas con conteo aproximado |
| `GET` | `/preview` | Vista pública del mapa |
| `POST` | `/api/v1/track/view` | Tracking de visualizaciones (opcional) |

### Autenticación (JWT)

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/api/v1/auth/login` | Obtener token (`grant_type=password`) |
| `GET` | `/api/v1/auth/me` | Perfil del usuario actual |
| `POST` | `/api/v1/auth/change-password` | Cambiar contraseña |

### Admin (requiere token)

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/api/v1/admin/stats` | Estadísticas de uso y PostGIS |
| `POST` | `/api/v1/admin/upload` | Subir e importar archivo |
| `POST` | `/api/v1/admin/import/scan` | Importar todos los archivos en `data/` |
| `POST` | `/api/v1/admin/replace` | Limpiar DB e importar nuevo archivo |
| `POST` | `/api/v1/admin/clear` | Eliminar todas las capas |
| `GET` | `/api/v1/admin/jobs` | Listar jobs de importación |
| `GET` | `/api/v1/admin/jobs/{id}` | Estado de un job |

### Tiles (Martin vía Nginx)

| Ruta | Descripción |
|------|-------------|
| `GET /tiles/{tabla}` | TileJSON de la capa |
| `GET /tiles/{tabla}/{z}/{x}/{y}.pbf` | Vector tile MVT |

## Consumir el mapa desde una app externa

```javascript
import maplibregl from 'maplibre-gl';

const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tu-dominio.com/api/v1/style.json',
  center: [-72.92, 11.54],  // Riohacha, La Guajira
  zoom: 12,
  pitch: 45,
  antialias: true
});
```

El style.json genera automáticamente:
- Fuente OSM como raster base
- Una source vector por cada capa en PostGIS
- Capas de polígonos con colores únicos por capa
- Capas 3D (construcciones) desde zoom 13+
- Capas de líneas (nomenclatura vial)
- Etiquetas para sectores, veredas, terrenos

## Estructura del proyecto

```
├── docker-compose.yml      # Stack completo (PostGIS, Martin, Manager, Nginx)
├── Dockerfile               # Imagen del manager FastAPI
├── nginx.conf               # Proxy con gzip, CORS, caché de tiles
├── import_data.sh           # Script de importación paralela desde terminal
├── optimize_db.sh           # Optimización de tablas PostGIS
├── download_assets.sh       # Descarga de fuentes y assets
├── .env                     # Variables de entorno
│
├── manager/
│   ├── main.py              # API FastAPI (estilos, auth, jobs, admin)
│   ├── ingest.py            # Motor de ingesta con ogr2ogr
│   ├── requirements.txt     # Dependencias Python
│   ├── Dockerfile           # Dockerfile del manager
│   └── static/
│       ├── index.html       # Panel admin (MapLibre + dashboard)
│       └── public.html      # Vista pública del mapa
│
├── data/
│   ├── mapa_guajira.gpkg    # Archivo de datos geoespaciales
│   ├── usage.json           # Tracking de uso
│   ├── users.json           # Usuarios (hashed)
│   └── layer_hashes.json    # MD5 por capa para importación incremental
│
├── styles/
│   └── basic.json           # Estilo estático de respaldo
│
└── fonts/
    └── Open Sans */         # Fuentes PBF para MapLibre
```

## Optimizaciones implementadas

| Optimización | Estado | Impacto |
|-------------|--------|---------|
| Pool de conexiones SQLAlchemy global | ✅ | Sin overhead de conexión por request |
| `COUNT(*)` → `pg_stat_user_tables` | ✅ | Listado de capas < 50 ms |
| Caché de style.json con TTL 5 min | ✅ | Style servido en < 10 ms |
| `track_usage` con `asyncio.Lock()` | ✅ | Sin race conditions en concurrencia |
| Importación asíncrona (BackgroundTasks) | ✅ | Servidor no se bloquea durante ingesta |
| Jobs API con progreso | ✅ | Feedback en tiempo real |
| Hash checking (MD5) por capa | ✅ | Importación incremental, skip si no hay cambios |
| Importación con `ogr2ogr` + `PG_USE_COPY` | ✅ | Carga masiva 10-50x más rápida que INSERT |
| Gzip en tiles protobuf | ✅ | Reducción 70-80% en ancho de banda |
| Caché de tiles 7 días en Nginx | ✅ | Tiles cacheados en cliente/proxy |
| Colores únicos por capa en style.json | ✅ | Distinción visual inmediata |
| 3D extrusiones con fallback 2D | ✅ | Edificios visibles en todo zoom |
| Vista pública sin autenticación | ✅ | Compartir mapa sin login |
| Limpieza automática WAL de GPKG | ✅ | Evita corrupción en imports fallidos |
| `ANALYZE` post-importación | ✅ | Estadísticas actualizadas para el planificador |

## Próximas mejoras

### FASE 1 — Correcciones inmediatas (completadas)
- [x] Engine SQLAlchemy global con pool
- [x] COUNT aproximado vía `pg_stat_user_tables`
- [x] Caché de estilo en memoria con TTL
- [x] `track_usage` en memoria con flush periódico
- [x] Ingesta con ogr2ogr nativo en vez de GeoPandas
- [x] Jobs asíncronos con progreso
- [x] Hash checking para importación incremental
- [x] Importación paralela con `xargs -P`
- [x] Auto-indexación GIST post-importación
- [x] Limpieza de dependencias pesadas (GeoPandas, Fiona)

### FASE 2 — Alta prioridad
- [ ] **TTL de caché de tiles en Nginx configurable** — actualmente 7 días fijo
- [ ] **Rate limiting** — proteger endpoints públicos de abuso
- [ ] **HTTPS/TLS** — certbot o Cloudflare para producción
- [ ] **Health checks** — monitoreo de estado de cada servicio
- [ ] **Pruebas de carga** — validar rendimiento con usuarios concurrentes

### FASE 3 — Media prioridad
- [ ] **Redis** — caché compartida y cola de jobs persistente
- [ ] **pg_partman** — particionamiento de tablas grandes
- [ ] **Importación diferencial (UPSERT)** — detectar cambios por clave predial
- [ ] **Tablas versionadas** — `_v1`, `_v2` con swap atómico para zero-downtime
- [ ] **Optimización de `work_mem`** para operaciones geométricas pesadas

### FASE 4 — Baja prioridad
- [ ] **WebSocket** para jobs en tiempo real (en vez de polling cada 2s)
- [ ] **Panel de métricas** — chart.js con requests por hora, capas más consultadas
- [ ] **Exportación** — descargar capas en GeoJSON/GPKG desde el panel
- [ ] **Multi-tenant** — proyectos separados con su propio espacio de capas
- [ ] **CLI tool** — interfaz de línea de comandos para operaciones comunes

## Troubleshooting

### "No se ven los datos en el mapa"

1. Verificar que las capas están en PostGIS: `curl http://localhost/api/v1/layers`
2. Revisar que la importación se completó: `curl http://localhost/api/v1/admin/jobs`
3. Forzar re-importación: `curl -X POST http://localhost/api/v1/admin/import/scan?force=true`
4. Ver errores: `docker compose logs manager`

### Error de GPKG lockeado

Si hay procesos `ogr2ogr` zombies:
```bash
killall ogr2ogr
rm -f data/*.gpkg-shm data/*.gpkg-wal
```

### Reconstruir desde cero

```bash
docker compose down -v          # Elimina volúmenes (incluye DB)
rm -f data/layer_hashes.json    # Olvida hashes de importación
docker compose up -d            # Reconstruye
curl -X POST .../import/scan?force=true
```

## Rendimiento esperado

| Operación | Antes | Después |
|-----------|-------|---------|
| Importar GPKG 3 GB (9 capas) | 30-60 min | 8-15 min |
| RAM durante importación | 8-12 GB | 1-2 GB |
| Listar capas | 2-5 s | < 50 ms |
| Servir style.json | 200-500 ms | < 10 ms |
| Disponibilidad durante importación | 0% | 100% |

## Licencia

Uso interno — NeuralJira
