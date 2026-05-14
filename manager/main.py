import os, json, shutil
from datetime import datetime, timedelta, timezone
from typing import Optional

import uvicorn
import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, status, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy import create_engine, text

SECRET_KEY = os.getenv("SECRET_KEY", "neuraljira-change-this-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://mapengine:mapengine123@db:5432/mapdb")
SYNC_DB_URL = DATABASE_URL.replace("+asyncpg", "")
MARTIN_URL = os.getenv("MARTIN_URL", "http://localhost:3000")

USERS_PATH = "data/users.json"
USAGE_PATH = "data/usage.json"
DATA_DIR = "data"

os.makedirs(DATA_DIR, exist_ok=True)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

app = FastAPI(title="NeuralJira Map Engine API", version="2.0.0",
              description="Map-as-a-Service con autenticación y administración.")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=True
)

if os.path.exists("fonts"):
    app.mount("/fonts", StaticFiles(directory="fonts"), name="fonts")

# ─── Users ──────────────────────────────────────────────────────────────────

def load_users():
    try:
        with open(USERS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_users(users):
    with open(USERS_PATH, "w") as f:
        json.dump(users, f, indent=2)

def init_admin():
    users = load_users()
    if "admin" not in users:
        users["admin"] = {
            "password": pwd_context.hash("admin123"),
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        save_users(users)
        print(">>> Admin user created: admin / admin123")

init_admin()

# ─── Usage Tracking ─────────────────────────────────────────────────────────

def load_usage():
    try:
        with open(USAGE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "total_requests": 0, "total_logins": 0, "total_uploads": 0,
            "by_endpoint": {}, "by_date": {}, "recent_activity": []
        }

def save_usage(usage):
    with open(USAGE_PATH, "w") as f:
        json.dump(usage, f, indent=2)

def track_usage(endpoint: str, username: str = "anonymous", details: dict = None):
    usage = load_usage()
    usage["total_requests"] += 1
    usage["by_endpoint"][endpoint] = usage["by_endpoint"].get(endpoint, 0) + 1
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage["by_date"][today] = usage["by_date"].get(today, 0) + 1
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "endpoint": endpoint, "user": username}
    if details: entry["details"] = details
    usage["recent_activity"].insert(0, entry)
    usage["recent_activity"] = usage["recent_activity"][:200]
    save_usage(usage)

# ─── Auth ────────────────────────────────────────────────────────────────────

class Token(BaseModel):
    access_token: str
    token_type: str

class UserOut(BaseModel):
    username: str
    role: str
    created_at: str

def create_access_token(username: str):
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "exp": expire, "iat": datetime.now(timezone.utc)},
                      SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
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

# ─── Helpers ────────────────────────────────────────────────────────────────

def process_gdf(df, name, engine):
    import geopandas as gpd
    if not isinstance(df, gpd.GeoDataFrame) or len(df) == 0:
        return None
    df['geometry'] = df.geometry.make_valid()
    if df.crs is None:
        df.set_crs("EPSG:4326", allow_override=True, inplace=True)
    elif df.crs != "EPSG:4326":
        df = df.to_crs("EPSG:4326")
    df = df[(df.geometry.notna()) & (~df.geometry.is_empty)]
    if len(df) == 0:
        return None
    table_name = name.lower().replace(" ", "_")
    try:
        df.to_postgis(table_name, engine, if_exists='replace', index=False)
        return table_name
    except Exception as e:
        print(f"PostGIS Import Error ({name}): {e}")
        return None

# ─── Public Endpoints ───────────────────────────────────────────────────────

@app.get("/")
async def root():
    return RedirectResponse(url="/docs")

@app.get("/preview", response_class=HTMLResponse)
async def preview():
    return HTMLResponse("""<!DOCTYPE html><html><head><title>NeuralJira 3D Map Engine</title>
<script src="https://unpkg.com/maplibre-gl@3.x/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@3.x/dist/maplibre-gl.css" rel="stylesheet" />
<style>body{margin:0;background:#020617;height:100vh;font-family:'Inter',system-ui,sans-serif}#map{height:100%;width:100%}.overlay{position:absolute;top:20px;left:20px;z-index:10;background:rgba(15,23,42,0.8);padding:24px;border-radius:16px;color:white;backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.1);box-shadow:0 20px 25px -5px rgba(0,0,0,0.5);width:280px}.btn{background:#38bdf8;color:#0f172a;border:none;padding:10px 15px;border-radius:8px;font-weight:600;cursor:pointer;transition:all .2s}.btn:hover{background:#7dd3fc}h2{margin:0 0 8px;font-size:20px;color:#38bdf8}p{margin:0;font-size:14px;opacity:.8}.badge{display:inline-block;background:#1e293b;padding:4px 8px;border-radius:4px;font-size:10px;margin-top:10px;border:1px solid #334155}</style></head>
<body><div class=overlay><h2>NeuralJira MaaS</h2><p>Infraestructura Vectorial de Alta Precisión.</p><div class=badge>Motor 3D Activo</div><br><a href=/admin style=display:inline-block;margin-top:12px;color:#38bdf8;font-size:12px>Panel Admin →</a></div><div id=map></div>
<script>const map=new maplibregl.Map({container:'map',style:'/api/v1/style.json',center:[-72.92,11.54],zoom:16,pitch:45,bearing:-17,antialias:true,hash:true});map.addControl(new maplibregl.NavigationControl({visualizePitch:true}));map.addControl(new maplibregl.FullscreenControl());map.on('load',()=>{map.setLight({anchor:'viewport',color:'white',intensity:.4})});</script></body></html>""")

@app.get("/api/v1/style.json")
async def get_style(request: Request):
    engine = create_engine(SYNC_DB_URL)
    host = request.url.hostname
    scheme = request.url.scheme
    client_martin = f"{scheme}://{host}:3000"
    base_url = f"{scheme}://{host}:{request.url.port}"
    style = {
        "version": 8, "name": "NeuralJira 3D Premium",
        "sources": {
            "osm": {"type": "raster", "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                    "tileSize": 256, "attribution": "&copy; OpenStreetMap contributors"}
        },
        "glyphs": f"{base_url}/fonts/{{fontstack}}/{{range}}.pbf",
        "layers": [{"id": "osm-layer", "type": "raster", "source": "osm",
                     "minzoom": 0, "maxzoom": 19, "paint": {"raster-opacity": 0.3}}]
    }
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT f_table_name, type FROM geometry_columns WHERE f_table_schema = 'public'"))
            layers = list(rows)
            for row in layers:
                lid, gtype = row[0], row[1]
                style["sources"][lid] = {"type": "vector", "url": f"{client_martin}/{lid}"}
                if "POLYGON" in gtype and "constru" not in lid:
                    style["layers"].append({"id": f"{lid}-fill", "type": "fill", "source": lid,
                        "source-layer": lid,
                        "paint": {"fill-color": "#e2e8f0", "fill-opacity": 0.2, "fill-outline-color": "rgba(255,255,255,0.1)"}})
            for row in layers:
                lid = row[0]
                if "constru" in lid:
                    style["layers"].append({"id": f"{lid}-3d", "type": "fill-extrusion",
                        "source": lid, "source-layer": lid, "minzoom": 14,
                        "paint": {"fill-extrusion-color": ["coalesce",
                            ["interpolate", ["linear"], ["get", "numero_pisos"], 1, "#f97316", 3, "#fb923c", 5, "#ea580c"], "#f97316"],
                            "fill-extrusion-height": ["coalesce",
                                ["interpolate", ["linear"], ["get", "numero_pisos"], 0, 3.5, 1, 4, 2, 8, 5, 20], 4],
                            "fill-extrusion-base": 0, "fill-extrusion-opacity": 0.85}})
            for row in layers:
                lid = row[0]
                if any(x in lid.lower() for x in ["nomencl", "etiqueta", "label", "u_nomen"]):
                    style["layers"].append({"id": f"{lid}-label", "type": "symbol",
                        "source": lid, "source-layer": lid, "minzoom": 14,
                        "layout": {"text-field": ["coalesce", ["get", "TEXTO"], ["get", "texto"], ["get", "nombre"], ["get", "ETIQUETA"]],
                            "text-font": ["Open Sans Regular"], "text-size": 12,
                            "text-allow-overlap": False, "text-letter-spacing": 0.05},
                        "paint": {"text-color": "#ffffff", "text-halo-color": "#0f172a", "text-halo-width": 2}})
    except Exception as e:
        print(f"Style Error: {e}")
    return style

@app.get("/api/v1/layers")
async def list_layers():
    engine = create_engine(SYNC_DB_URL)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT f_table_name, type, srid FROM geometry_columns WHERE f_table_schema = 'public'"))
            layers = []
            for r in rows:
                count = conn.execute(text(f"SELECT count(*) FROM {r[0]}")).scalar()
                layers.append({"name": r[0], "type": r[1], "srid": r[2], "features": count})
            return {"status": "success", "layers": layers}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

# ─── Auth Endpoints ─────────────────────────────────────────────────────────

@app.post("/api/v1/auth/login", response_model=Token)
async def login(form: OAuth2PasswordRequestForm = Depends()):
    users = load_users()
    user = users.get(form.username)
    if not user or not pwd_context.verify(form.password, user["password"]):
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos")
    track_usage("login", form.username)
    usage = load_usage()
    usage["total_logins"] += 1
    save_usage(usage)
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

# ─── Admin Endpoints ────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    with open("static/index.html") as f:
        return HTMLResponse(f.read())

@app.get("/api/v1/admin/stats")
async def admin_stats(current_user: str = Depends(get_current_user)):
    usage = load_usage()
    engine = create_engine(SYNC_DB_URL)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT f_table_name FROM geometry_columns WHERE f_table_schema = 'public'"))
            layer_count = len(list(rows))
        total_features = 0
        with engine.connect() as conn:
            for r in conn.execute(text("SELECT f_table_name FROM geometry_columns WHERE f_table_schema = 'public'")):
                total_features += conn.execute(text(f"SELECT count(*) FROM {r[0]}")).scalar()
    except:
        layer_count = total_features = 0
    track_usage("admin_stats", current_user)
    return {
        "status": "success",
        "usage": usage,
        "postgis": {"layers": layer_count, "total_features": total_features},
        "server_uptime": ""
    }

@app.post("/api/v1/admin/upload")
async def upload_file(file: UploadFile = File(...), current_user: str = Depends(get_current_user)):
    import geopandas as gpd
    import fiona
    engine = create_engine(SYNC_DB_URL)
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    filepath = os.path.join(DATA_DIR, filename)

    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer, length=16*1024*1024)

    results = []
    errors = []
    try:
        if ext == '.gpkg':
            layers = fiona.listlayers(filepath)
            for layer in layers:
                try:
                    gdf = gpd.read_file(filepath, layer=layer, engine="pyogrio")
                    tbl = process_gdf(gdf, layer, engine)
                    if tbl: results.append(tbl)
                except Exception as e:
                    errors.append(f"{layer}: {e}")
        elif ext in ['.geojson', '.json']:
            gdf = gpd.read_file(filepath, engine="pyogrio")
            tbl = process_gdf(gdf, os.path.splitext(filename)[0], engine)
            if tbl: results.append(tbl)
        else:
            os.remove(filepath)
            raise HTTPException(status_code=400, detail=f"Formato no soportado: {ext}")
    except Exception as e:
        if os.path.exists(filepath): os.remove(filepath)
        raise HTTPException(status_code=500, detail=str(e))

    usage = load_usage()
    usage["total_uploads"] += 1
    save_usage(usage)
    track_usage("upload", current_user, {"file": filename, "layers": results})

    return {"status": "finished", "file": filename, "imported": results, "errors": errors}

@app.post("/api/v1/admin/import/scan")
async def scan_data(current_user: str = Depends(get_current_user)):
    import geopandas as gpd
    import fiona
    engine = create_engine(SYNC_DB_URL)
    results = []
    errors = []
    if not os.path.exists(DATA_DIR):
        raise HTTPException(status_code=400, detail="No data folder")
    for entry in os.listdir(DATA_DIR):
        path = os.path.join(DATA_DIR, entry)
        if not os.path.isfile(path): continue
        ext = os.path.splitext(entry)[1].lower()
        try:
            if ext == '.gpkg':
                for layer in fiona.listlayers(path):
                    try:
                        gdf = gpd.read_file(path, layer=layer, engine="pyogrio")
                        tbl = process_gdf(gdf, layer, engine)
                        if tbl: results.append(tbl)
                    except Exception as e:
                        errors.append(f"{entry}/{layer}: {e}")
            elif ext in ['.geojson', '.json']:
                gdf = gpd.read_file(path, engine="pyogrio")
                tbl = process_gdf(gdf, os.path.splitext(entry)[0], engine)
                if tbl: results.append(tbl)
        except Exception as e:
            errors.append(f"{entry}: {e}")
    track_usage("scan", current_user, {"imported": results})
    return {"status": "finished", "imported_layers": results, "errors": errors}

@app.post("/api/v1/admin/clear")
async def clear_data(current_user: str = Depends(get_current_user)):
    engine = create_engine(SYNC_DB_URL)
    dropped = []
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT f_table_name FROM geometry_columns WHERE f_table_schema = 'public'"))
        for r in rows:
            conn.execute(text(f"DROP TABLE IF EXISTS {r[0]} CASCADE"))
            dropped.append(r[0])
        conn.commit()
    track_usage("clear", current_user, {"dropped_tables": dropped})
    return {"status": "ok", "dropped": dropped}

@app.post("/api/v1/admin/replace")
async def replace_data(file: UploadFile = File(...), current_user: str = Depends(get_current_user)):
    import geopandas as gpd
    import fiona
    engine = create_engine(SYNC_DB_URL)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT f_table_name FROM geometry_columns WHERE f_table_schema = 'public'"))
        for r in rows:
            conn.execute(text(f"DROP TABLE IF EXISTS {r[0]} CASCADE"))
        conn.commit()

    filename = file.filename
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer, length=16*1024*1024)

    results = []
    errors = []
    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext == '.gpkg':
            for layer in fiona.listlayers(filepath):
                try:
                    gdf = gpd.read_file(filepath, layer=layer, engine="pyogrio")
                    tbl = process_gdf(gdf, layer, engine)
                    if tbl: results.append(tbl)
                except Exception as e:
                    errors.append(f"{layer}: {e}")
        elif ext in ['.geojson', '.json']:
            gdf = gpd.read_file(filepath, engine="pyogrio")
            tbl = process_gdf(gdf, os.path.splitext(filename)[0], engine)
            if tbl: results.append(tbl)
    except Exception as e:
        if os.path.exists(filepath): os.remove(filepath)
        raise HTTPException(status_code=500, detail=str(e))

    usage = load_usage()
    usage["total_uploads"] += 1
    save_usage(usage)
    track_usage("replace", current_user, {"file": filename, "imported": results})

    return {"status": "finished", "file": filename, "imported": results, "errors": errors}

# ─── Track ──────────────────────────────────────────────────────────────────

@app.post("/api/v1/track/view")
async def track_view():
    track_usage("map_view")
    return {"status": "ok"}

# ─── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
