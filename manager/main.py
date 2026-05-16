import os, json, uuid, time, asyncio, shutil, logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import uvicorn
import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, status, UploadFile, File, BackgroundTasks, Response
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
# pyrefly: ignore [missing-import]
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import create_engine, text, pool
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("map-engine")

# ─── Config ──────────────────────────────────────────────────────────────────

SECRET_KEY = os.getenv("SECRET_KEY", "neuraljira-change-this-key-to-32-bytes")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://mapengine:mapengine123@db:5432/mapdb")
SYNC_DB_URL = DATABASE_URL.replace("+asyncpg", "")
MARTIN_URL = os.getenv("MARTIN_URL", "http://localhost:3000")
PUBLIC_MARTIN_URL = os.getenv("PUBLIC_MARTIN_URL", "https://tiles.neuraljira.tech").rstrip("/")

USERS_PATH = "data/users.json"
USAGE_PATH = "data/usage.json"
DATA_DIR = "data"
STYLE_CACHE_TTL = 300  # segundos

os.makedirs(DATA_DIR, exist_ok=True)

# ─── Engine Global con Pool ──────────────────────────────────────────────────
# ✅ Una sola engine compartida por toda la app (no una por request)

engine = create_engine(
    SYNC_DB_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
    poolclass=pool.QueuePool,
)

# ─── Auth ─────────────────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

# ─── FastAPI ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title="NeuralJira Map Engine API",
    version="3.0.0",
    description="Map-as-a-Service — Optimizado para alto rendimiento.",
)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=True,
)

if os.path.exists("fonts"):
    app.mount("/fonts", StaticFiles(directory="fonts"), name="fonts")

# ─── Jobs Store (en memoria) ─────────────────────────────────────────────────
# Persiste durante la vida del proceso; suficiente para jobs de importación.

_jobs: dict[str, dict] = {}

def create_job() -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "id": job_id,
        "status": "pending",
        "progress": 0,
        "layer": None,
        "message": "En cola...",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "result": None,
    }
    return job_id

def update_job(job_id: str, **kwargs):
    if job_id in _jobs:
        _jobs[job_id].update(kwargs)

# ─── Style Cache ─────────────────────────────────────────────────────────────

_style_cache: dict = {"data": None, "ts": 0.0}

def invalidate_style_cache():
    _style_cache["ts"] = 0.0

# ─── Users ───────────────────────────────────────────────────────────────────

def load_users() -> dict:
    try:
        with open(USERS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_users(users: dict):
    with open(USERS_PATH, "w") as f:
        json.dump(users, f, indent=2)

def init_admin():
    users = load_users()
    if "admin" not in users:
        users["admin"] = {
            "password": pwd_context.hash("admin123"),
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        save_users(users)
        logger.info(">>> Admin user created: admin / admin123")

init_admin()

# ─── Usage Tracking (con Lock para evitar race conditions) ───────────────────

_usage_lock = asyncio.Lock()
_usage_cache: dict | None = None
_usage_dirty = False

def _read_usage_from_disk() -> dict:
    try:
        with open(USAGE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "total_requests": 0, "total_logins": 0, "total_uploads": 0,
            "by_endpoint": {}, "by_date": {}, "recent_activity": [],
        }

def _write_usage_to_disk(usage: dict):
    with open(USAGE_PATH, "w") as f:
        json.dump(usage, f, indent=2)

async def track_usage(endpoint: str, username: str = "anonymous", details: dict = None):
    global _usage_cache, _usage_dirty
    async with _usage_lock:
        if _usage_cache is None:
            _usage_cache = _read_usage_from_disk()
        _usage_cache["total_requests"] += 1
        _usage_cache["by_endpoint"][endpoint] = _usage_cache["by_endpoint"].get(endpoint, 0) + 1
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _usage_cache["by_date"][today] = _usage_cache["by_date"].get(today, 0) + 1
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "endpoint": endpoint, "user": username}
        if details:
            entry["details"] = details
        _usage_cache["recent_activity"].insert(0, entry)
        _usage_cache["recent_activity"] = _usage_cache["recent_activity"][:200]
        _usage_dirty = True

async def _flush_usage_periodically():
    """Background task: guarda usage a disco cada 60 segundos."""
    global _usage_dirty
    while True:
        await asyncio.sleep(60)
        async with _usage_lock:
            if _usage_dirty and _usage_cache is not None:
                _write_usage_to_disk(_usage_cache)
                _usage_dirty = False

@app.on_event("startup")
async def startup():
    asyncio.create_task(_flush_usage_periodically())
    logger.info("✅ Map Engine v3 iniciado — pool de conexiones activo")

@app.on_event("shutdown")
async def shutdown():
    global _usage_dirty
    async with _usage_lock:
        if _usage_dirty and _usage_cache is not None:
            _write_usage_to_disk(_usage_cache)

# ─── Auth Helpers ─────────────────────────────────────────────────────────────

class Token(BaseModel):
    access_token: str
    token_type: str

class UserOut(BaseModel):
    username: str
    role: str
    created_at: str

def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None

async def get_current_user(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    username = verify_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return username

# ─── Public Endpoints ────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return RedirectResponse(url="/docs")

@app.get("/preview", response_class=HTMLResponse)
async def preview():
    with open("static/public.html") as f:
        return HTMLResponse(f.read())

@app.get("/api/v1/config")
async def get_config():
    return {"martin_url": "/tiles"}

def _layer_color(name: str, offset: int = 0) -> str:
    # Paleta Minimalista y Profesional (Slate, Blue-Grey, Stone)
    name_l = name.lower()
    if "constru" in name_l:
        return "#94a3b8" # Slate 400 (Muted Blue-Grey)
    if "terreno" in name_l:
        return "#cbd5e1" # Slate 200 (Light Grey)
    if "sector" in name_l or "barrio" in name_l:
        return "#e2e8f0" # Slate 100 (Very Light)
    if "vereda" in name_l:
        return "#f1f5f9" # Slate 50 (Near White)
    
    # Fallback a colores suaves
    h = (hash(name) + offset) % 360
    return f"hsl({h}, 20%, 75%)"

def _layer_outline(name: str) -> str:
    return "rgba(71, 85, 105, 0.2)" # Slate 700 con mucha transparencia

@app.get("/api/v1/style.json")
async def get_style(request: Request):
    now = time.time()
    if _style_cache["data"] and (now - _style_cache["ts"]) < STYLE_CACHE_TTL:
        return _style_cache["data"]

    host = request.url.hostname
    scheme = request.url.scheme
    port = request.url.port
    forwarded_host = request.headers.get("x-forwarded-host")
    forwarded_proto = request.headers.get("x-forwarded-proto", scheme)
    base_url = f"{forwarded_proto}://{forwarded_host}" if forwarded_host else f"{scheme}://{host}{f':{port}' if port else ''}"

    # Usar URL absoluta para evitar problemas con Web Workers
    current_martin_url = f"{base_url}/tiles"

    style = {
        "version": 8,
        "name": "NeuralJira Optimized",
        "metadata": {"maputnik:renderer": "mlgljs"},
        "sources": {
            "osm": {
                "type": "raster",
                "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                "tileSize": 256,
                "attribution": "&copy; OpenStreetMap contributors",
                "maxzoom": 19
            }
        },
        "glyphs": f"{base_url}/fonts/{{fontstack}}/{{range}}.pbf",
        "layers": [
            {
                "id": "osm-layer",
                "type": "raster",
                "source": "osm",
                "minzoom": 0,
                "maxzoom": 19,
                "paint": {"raster-opacity": 0.4}
            }
        ],
    }

    try:
        with engine.connect() as conn:
            rows = list(conn.execute(text("""
                SELECT gc.f_table_name, gc.type
                FROM geometry_columns gc
                LEFT JOIN pg_stat_user_tables ps ON ps.relname = gc.f_table_name AND ps.schemaname = gc.f_table_schema
                WHERE gc.f_table_schema = 'public'
                  AND gc.f_table_name NOT LIKE '%_staging'
                  AND COALESCE(ps.n_live_tup, 0) > 0
            """)))

            for lid, gtype in rows:
                style["sources"][lid] = {
                    "type": "vector",
                    "tiles": [f"{current_martin_url}/{lid}/{{z}}/{{x}}/{{y}}"],
                    "minzoom": 0,
                    "maxzoom": 22
                }

            gtype_upper = {lid: gtype.upper() for lid, gtype in rows}

            polygon_layers = [lid for lid, gu in gtype_upper.items() if "POLYGON" in gu]
            line_layers = [lid for lid, gu in gtype_upper.items() if "LINESTRING" in gu or "LINE" in gu]
            point_layers = [lid for lid, gu in gtype_upper.items() if "POINT" in gu]
            geometry_layers = [lid for lid, gu in gtype_upper.items() if "GEOMETRY" in gu]

            construction_layers = [lid for lid in polygon_layers if "constru" in lid.lower()]
            base_polygons = [lid for lid in polygon_layers if "constru" not in lid.lower()]

            for lid in base_polygons:
                c = _layer_color(lid)
                style["layers"].append({
                    "id": f"{lid}-fill",
                    "type": "fill",
                    "source": lid,
                    "source-layer": lid,
                    "minzoom": 10,
                    "paint": {
                        "fill-color": c,
                        "fill-opacity": 0.55,
                        "fill-outline-color": _layer_outline(lid),
                    },
                })

            for lid in construction_layers:
                c = _layer_color(lid, 200)
                style["layers"].append({
                    "id": f"{lid}-fill-2d",
                    "type": "fill",
                    "source": lid,
                    "source-layer": lid,
                    "maxzoom": 13,
                    "minzoom": 10,
                    "paint": {
                        "fill-color": c,
                        "fill-opacity": 0.4,
                        "fill-outline-color": _layer_outline(lid),
                    },
                })
                style["layers"].append({
                    "id": f"{lid}-3d",
                    "type": "fill-extrusion",
                    "source": lid,
                    "source-layer": lid,
                    "minzoom": 13,
                    "paint": {
                        "fill-extrusion-color": [
                            "interpolate", ["linear"], 
                            ["coalesce", ["get", "numero_pisos"], ["get", "pisos"], 1],
                            1, "#f8fafc",
                            3, "#f1f5f9",
                            5, "#e2e8f0",
                            10, "#cbd5e1"
                        ],
                        "fill-extrusion-height": [
                            "coalesce",
                            ["*", ["get", "numero_pisos"], 3.8],
                            ["*", ["get", "pisos"], 3.8],
                            3.8
                        ],
                        "fill-extrusion-base": 0,
                        "fill-extrusion-opacity": 0.8
                    },
                })

            for lid in line_layers:
                c = "#475569" # Slate 700 para líneas
                width = 1.0 if "nomenclatura" in lid.lower() else 1.5
                style["layers"].append({
                    "id": f"{lid}-line",
                    "type": "line",
                    "source": lid,
                    "source-layer": lid,
                    "minzoom": 12,
                    "paint": {
                        "line-color": c,
                        "line-width": width,
                        "line-opacity": 0.6,
                    },
                })

            for lid in point_layers:
                c = "#64748b" # Slate 500
                style["layers"].append({
                    "id": f"{lid}-circle",
                    "type": "circle",
                    "source": lid,
                    "source-layer": lid,
                    "minzoom": 14,
                    "paint": {
                        "circle-color": c,
                        "circle-radius": 3,
                        "circle-stroke-width": 1,
                        "circle-stroke-color": "#fff",
                        "circle-opacity": 0.7,
                    },
                })

            for lid in geometry_layers:
                c = "#94a3b8"
                style["layers"].append({
                    "id": f"{lid}-line",
                    "type": "line",
                    "source": lid,
                    "source-layer": lid,
                    "minzoom": 13,
                    "paint": {
                        "line-color": c,
                        "line-width": 1,
                        "line-opacity": 0.5,
                    },
                })

            for lid, gtype in rows:
                label_keywords = ["nomencl", "etiqueta", "label", "lugar", "poi", "vereda", "sector", "terreno"]
                if any(x in lid.lower() for x in label_keywords):
                    style["layers"].append({
                        "id": f"{lid}-label",
                        "type": "symbol",
                        "source": lid,
                        "source-layer": lid,
                        "minzoom": 15,
                        "layout": {
                            "text-field": [
                                "coalesce",
                                ["get", "texto"], ["get", "nombre"], ["get", "label"],
                                ["get", "name"], ["get", "numero_predial"], ["get", "codigo"]
                            ],
                            "text-font": ["Open Sans Regular"],
                            "text-size": [
                                "interpolate", ["linear"], ["zoom"],
                                15, 9,
                                18, 12
                            ],
                            "text-letter-spacing": 0.05,
                            "text-max-width": 8,
                            "symbol-placement": "point",
                            "text-variable-anchor": ["top", "bottom", "left", "right"],
                            "text-padding": 2
                        },
                        "paint": {
                            "text-color": "#334155", # Slate 800
                            "text-halo-color": "rgba(255, 255, 255, 0.8)",
                            "text-halo-width": 1.5
                        },
                    })
    except Exception as e:
        logger.error(f"Error generando style.json: {e}")

    _style_cache.update({"data": style, "ts": now})
    return style

# ─── Tiles Proxy (Unified Gateway) ──────────────────────────────────────────

_http_client = httpx.AsyncClient(base_url=MARTIN_URL, timeout=30.0)

@app.get("/tiles/{path:path}")
async def proxy_tiles(path: str, request: Request):
    """
    Proxy que reenvía peticiones de tiles a Martin.
    Esto soluciona problemas de CORS y prefijos de URL.
    """
    url = f"/{path}"
    if request.query_params:
        url += f"?{request.query_params}"
    
    try:
        # Reenviar la petición a Martin (interno en Docker)
        rp_resp = await _http_client.get(url)
        
        # Devolver la respuesta con los mismos headers (especialmente content-type)
        return Response(
            content=rp_resp.content,
            status_code=rp_resp.status_code,
            headers={k: v for k, v in rp_resp.headers.items() if k.lower() in ["content-type", "content-encoding", "cache-control"]}
        )
    except Exception as e:
        logger.error(f"Error en proxy de tiles: {e}")
        raise HTTPException(status_code=502, detail="Error conectando con el servidor de tiles")



@app.get("/api/v1/layers")
async def list_layers():
    """
    ✅ Usa pg_stat_user_tables para conteos aproximados instantáneos
    en lugar de SELECT COUNT(*) por tabla (que puede tardar segundos).
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT
                    gc.f_table_name,
                    gc.type,
                    gc.srid,
                    COALESCE(ps.n_live_tup, 0)                        AS features,
                    pg_size_pretty(
                        pg_total_relation_size(
                            quote_ident(gc.f_table_name)::regclass
                        )
                    )                                                  AS size_pretty
                FROM geometry_columns gc
                LEFT JOIN pg_stat_user_tables ps
                       ON ps.relname = gc.f_table_name
                      AND ps.schemaname = gc.f_table_schema
                WHERE gc.f_table_schema = 'public'
                ORDER BY gc.f_table_name
            """))
            layers = [
                {"name": r[0], "type": r[1], "srid": r[2],
                 "features": r[3], "size": r[4]}
                for r in rows
            ]
        return {"status": "success", "layers": layers}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ─── Auth Endpoints ───────────────────────────────────────────────────────────

@app.post("/api/v1/auth/login", response_model=Token)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    users = load_users()
    user = users.get(form.username)
    if not user or not pwd_context.verify(form.password, user["password"]):
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos")
    await track_usage("login", form.username)
    async with _usage_lock:
        if _usage_cache:
            _usage_cache["total_logins"] += 1
    return {"access_token": create_access_token(form.username), "token_type": "bearer"}

@app.get("/api/v1/auth/me", response_model=UserOut)
async def me(username: str = Depends(get_current_user)):
    users = load_users()
    u = users.get(username)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut(username=username, role=u["role"], created_at=u.get("created_at", ""))

@app.post("/api/v1/auth/change-password")
async def change_password(old: str, new: str, username: str = Depends(get_current_user)):
    users = load_users()
    u = users.get(username)
    if not u or not pwd_context.verify(old, u["password"]):
        raise HTTPException(status_code=400, detail="Contraseña actual incorrecta")
    u["password"] = pwd_context.hash(new)
    save_users(users)
    return {"status": "ok"}

# ─── Admin Endpoints ──────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    with open("static/index.html") as f:
        return HTMLResponse(f.read())

@app.get("/api/v1/admin/stats")
async def admin_stats(current_user: str = Depends(get_current_user)):
    async with _usage_lock:
        usage_snapshot = dict(_usage_cache) if _usage_cache else _read_usage_from_disk()

    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT
                    COUNT(*)                          AS layer_count,
                    COALESCE(SUM(ps.n_live_tup), 0)  AS total_features
                FROM geometry_columns gc
                LEFT JOIN pg_stat_user_tables ps
                       ON ps.relname = gc.f_table_name
                      AND ps.schemaname = gc.f_table_schema
                WHERE gc.f_table_schema = 'public'
            """)).fetchone()
            layer_count = row[0]
            total_features = row[1]
    except Exception:
        layer_count = total_features = 0

    await track_usage("admin_stats", current_user)
    return {
        "status": "success",
        "usage": usage_snapshot,
        "postgis": {"layers": layer_count, "total_features": total_features},
    }

# ─── Jobs API ─────────────────────────────────────────────────────────────────

@app.get("/api/v1/admin/jobs/{job_id}")
async def get_job(job_id: str, current_user: str = Depends(get_current_user)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job

@app.get("/api/v1/admin/jobs")
async def list_jobs(current_user: str = Depends(get_current_user)):
    return {"jobs": list(_jobs.values())}

# ─── Importación Async (no bloquea FastAPI) ───────────────────────────────────

async def _run_ingest_job(job_id: str, filepath: str, force: bool = False, mode: str = "replace"):
    """Corre en background — importa sin bloquear el servidor."""
    from ingest import ingest_file

    update_job(job_id, status="running", message=f"Procesando archivo ({mode})...")

    def progress(layer, current, total, msg):
        pct = int((current / max(total, 1)) * 100)
        update_job(job_id, layer=layer, progress=pct, message=msg)

    try:
        from fastapi.concurrency import run_in_threadpool
        result = await run_in_threadpool(
            ingest_file,
            filepath=str(filepath),
            db_url=SYNC_DB_URL,
            progress_cb=progress,
            force=force,
            mode=mode,
        )
        invalidate_style_cache()
        num_imported = len([r for r in result.get('results', []) if r.get('status') == 'imported'])
        update_job(
            job_id,
            status="done",
            progress=100,
            message=f"✅ Completado: {num_imported} capas procesadas",
            finished_at=datetime.now(timezone.utc).isoformat(),
            result=result,
        )
        logger.info(f"Job {job_id} completado: {result}")
    except Exception as e:
        update_job(
            job_id,
            status="error",
            message=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.error(f"Job {job_id} falló: {e}")


@app.post("/api/v1/admin/upload")
async def upload_file(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    force: bool = False,
    mode: str = "replace",
    current_user: str = Depends(get_current_user),
):
    """
    ✅ Sube el archivo y lanza la importación como job en background.
    Retorna inmediatamente con un job_id para consultar el progreso.
    """
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".gpkg", ".geojson", ".json", ".dxf"):
        raise HTTPException(status_code=400, detail=f"Formato no soportado: {ext}")

    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "wb") as buf:
        shutil.copyfileobj(file.file, buf, length=16 * 1024 * 1024)

    job_id = create_job()
    background_tasks.add_task(_run_ingest_job, job_id, filepath, force, mode)

    async with _usage_lock:
        if _usage_cache:
            _usage_cache["total_uploads"] += 1
    await track_usage("upload", current_user, {"file": filename, "job_id": job_id})

    return {
        "status": "accepted",
        "job_id": job_id,
        "poll_url": f"/api/v1/admin/jobs/{job_id}",
        "message": "Importación iniciada en segundo plano. Consulta poll_url para ver el progreso.",
    }


@app.post("/api/v1/admin/import/scan")
async def scan_data(
    background_tasks: BackgroundTasks,
    force: bool = False,
    mode: str = "replace",
    current_user: str = Depends(get_current_user),
):
    """Importa todos los archivos GPKG/GeoJSON del directorio data/."""
    files = [
        os.path.join(DATA_DIR, f)
        for f in os.listdir(DATA_DIR)
        if os.path.splitext(f)[1].lower() in (".gpkg", ".geojson", ".json", ".dxf")
        and f not in ("usage.json", "users.json", "layer_hashes.json")
        and os.path.isfile(os.path.join(DATA_DIR, f))
    ]
    if not files:
        raise HTTPException(status_code=400, detail="No hay archivos de datos en /data")

    job_ids = []
    for filepath in files:
        job_id = create_job()
        background_tasks.add_task(_run_ingest_job, job_id, filepath, force, mode)
        job_ids.append({"file": os.path.basename(filepath), "job_id": job_id})

    await track_usage("scan", current_user)
    return {"status": "accepted", "jobs": job_ids}


@app.post("/api/v1/admin/replace")
async def replace_data(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: str = Depends(get_current_user),
):
    """Limpia la DB y sube un nuevo archivo en segundo plano."""
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".gpkg", ".geojson", ".json", ".dxf"):
        raise HTTPException(status_code=400, detail=f"Formato no soportado: {ext}")

    # Limpiar tablas existentes
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT f_table_name FROM geometry_columns WHERE f_table_schema = 'public'"
            ))
            for r in rows:
                conn.execute(text(f"DROP TABLE IF EXISTS {r[0]} CASCADE"))
            conn.commit()
        invalidate_style_cache()
    except Exception as e:
        logger.warning(f"Error al limpiar DB: {e}")

    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "wb") as buf:
        shutil.copyfileobj(file.file, buf, length=16 * 1024 * 1024)

    job_id = create_job()
    background_tasks.add_task(_run_ingest_job, job_id, filepath, force=True)

    async with _usage_lock:
        if _usage_cache:
            _usage_cache["total_uploads"] += 1
    await track_usage("replace", current_user, {"file": filename, "job_id": job_id})

    return {
        "status": "accepted",
        "job_id": job_id,
        "poll_url": f"/api/v1/admin/jobs/{job_id}",
        "message": "DB limpiada. Importación en segundo plano.",
    }


@app.post("/api/v1/admin/clear")
async def clear_data(current_user: str = Depends(get_current_user)):
    dropped = []
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT f_table_name FROM geometry_columns WHERE f_table_schema = 'public'"
        ))
        for r in rows:
            conn.execute(text(f"DROP TABLE IF EXISTS {r[0]} CASCADE"))
            dropped.append(r[0])
        conn.commit()
    invalidate_style_cache()
    await track_usage("clear", current_user, {"dropped_tables": dropped})
    return {"status": "ok", "dropped": dropped}

# ─── Track ────────────────────────────────────────────────────────────────────

@app.post("/api/v1/track/view")
async def track_view():
    await track_usage("map_view")
    return {"status": "ok"}

# ─── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
