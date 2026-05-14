import os
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from fastapi.staticfiles import StaticFiles

app = FastAPI(
    title="NeuralJira Map Engine API",
    description="Map-as-a-Service Infrastructure.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True
)

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://mapengine:mapengine123@db:5432/mapdb")
SYNC_DB_URL = DATABASE_URL.replace("+asyncpg", "")
MARTIN_URL = os.getenv("MARTIN_URL", "http://localhost:3000")

# Mount fonts and static files
if os.path.exists("fonts"):
    app.mount("/fonts", StaticFiles(directory="fonts"), name="fonts")

def process_gdf(df, name, engine):
    import geopandas as gpd
    """Processes GeoDataFrame: corrects geometries, sets CRS, and uploads to PostGIS."""
    if not isinstance(df, gpd.GeoDataFrame) or len(df) == 0:
        return None
    
    df['geometry'] = df.geometry.make_valid()
    
    if df.crs is None:
        df.set_crs("EPSG:4326", allow_override=True, inplace=True)
    elif df.crs != "EPSG:4326":
        df = df.to_crs("EPSG:4326")
    
    df = df[df.geometry.notnull() & (~df.geometry.is_empty)]
    if len(df) == 0: return None

    table_name = name.lower().replace(" ", "_")
    try:
        df.to_postgis(table_name, engine, if_exists='replace', index=False)
        return table_name
    except Exception as e:
        print(f"PostGIS Import Error ({name}): {e}")
        return None

@app.get("/preview", response_class=HTMLResponse)
async def preview():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>NeuralJira 3D Map Engine</title>
        <script src="https://unpkg.com/maplibre-gl@3.x/dist/maplibre-gl.js"></script>
        <link href="https://unpkg.com/maplibre-gl@3.x/dist/maplibre-gl.css" rel="stylesheet" />
        <style>
            body { margin: 0; background: #020617; height: 100vh; font-family: 'Inter', system-ui, sans-serif; }
            #map { height: 100%; width: 100%; }
            .overlay { 
                position: absolute; top: 20px; left: 20px; z-index: 10; 
                background: rgba(15, 23, 42, 0.8); padding: 24px; border-radius: 16px; 
                color: white; backdrop-filter: blur(12px); border: 1px solid rgba(255,255,255,0.1); 
                box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); width: 280px;
            }
            .controls { position: absolute; bottom: 30px; right: 10px; z-index: 10; display: flex; flex-direction: column; gap: 8px; }
            .btn { background: #38bdf8; color: #0f172a; border: none; padding: 10px 15px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
            .btn:hover { background: #7dd3fc; transform: translateY(-2px); }
            h2 { margin: 0 0 8px 0; font-size: 20px; color: #38bdf8; letter-spacing: -0.025em; }
            p { margin: 0; font-size: 14px; opacity: 0.8; line-height: 1.5; }
            .badge { display: inline-block; background: #1e293b; padding: 4px 8px; border-radius: 4px; font-size: 10px; margin-top: 10px; text-transform: uppercase; letter-spacing: 0.05em; border: 1px solid #334155; }
        </style>
    </head>
    <body>
        <div class="overlay">
            <h2>NeuralJira MaaS</h2>
            <p>Infraestructura Vectorial de Alta Precisión. Datos Catastrales de Colombia (IGAC).</p>
            <div class="badge">Motor 3D Activo</div>
        </div>
        <div id="map"></div>
        <script>
            const map = new maplibregl.Map({
                container: 'map',
                style: '/api/v1/style.json',
                center: [-72.92, 11.54],
                zoom: 16,
                pitch: 45, // Inclinación inicial
                bearing: -17, // Rotación inicial
                antialias: true,
                hash: true,
                maxZoom: 22 // Super Zoom
            });
            map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }));
            map.addControl(new maplibregl.FullscreenControl());
            
            map.on('load', () => {
                // Configuración de atmósfera y luces para 3D
                map.setLight({ anchor: 'viewport', color: 'white', intensity: 0.4 });
            });
        </script>
    </body>
    </html>
    """

@app.get("/")
async def root():
    return RedirectResponse(url="/docs")

@app.get("/api/v1/layers")
async def list_layers():
    engine = create_engine(SYNC_DB_URL)
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT f_table_name, type, srid FROM geometry_columns WHERE f_table_schema = 'public'"))
            layers = []
            for row in result:
                count = conn.execute(text(f"SELECT count(*) FROM {row[0]}")).scalar()
                layers.append({"id": row[0], "type": row[1], "features": count})
            return {"status": "success", "layers": layers}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/v1/style.json")
async def get_style(request: Request):
    engine = create_engine(SYNC_DB_URL)
    host = request.url.hostname
    scheme = request.url.scheme
    client_martin = f"{scheme}://{host}:3000"
    base_url = f"{scheme}://{host}:{request.url.port}"

    style = {
        "version": 8,
        "name": "NeuralJira 3D Premium",
        "metadata": {"maputnik:renderer": "mlgljs"},
        "sources": {
            "osm": {
                "type": "raster",
                "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                "tileSize": 256,
                "attribution": "&copy; OpenStreetMap contributors"
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
                "paint": {"raster-opacity": 0.3} # Base muy tenue para que el 3D destaque
            }
        ]
    }

    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT f_table_name, type FROM geometry_columns WHERE f_table_schema = 'public'"))
            layers = list(result)
            
            # Step 1: Background Polygons (Terrenos/Sectores)
            for row in layers:
                layer_id = row[0]
                geom_type = row[1]
                style["sources"][layer_id] = {"type": "vector", "url": f"{client_martin}/{layer_id}"}
                
                if "POLYGON" in geom_type and "constru" not in layer_id:
                    style["layers"].append({
                        "id": f"{layer_id}-fill",
                        "type": "fill",
                        "source": layer_id,
                        "source-layer": layer_id,
                        "paint": {
                            "fill-color": "#e2e8f0",
                            "fill-opacity": 0.2,
                            "fill-outline-color": "rgba(255,255,255,0.1)"
                        }
                    })

            # Step 2: 3D Buildings (The Star Layer)
            for row in layers:
                layer_id = row[0]
                if "constru" in layer_id:
                    style["layers"].append({
                        "id": f"{layer_id}-3d",
                        "type": "fill-extrusion",
                        "source": layer_id,
                        "source-layer": layer_id,
                        "minzoom": 14,
                        "paint": {
                            "fill-extrusion-color": [
                                "coalesce",
                                [
                                    "interpolate", ["linear"], ["get", "numero_pisos"],
                                    1, "#f97316",
                                    3, "#fb923c",
                                    5, "#ea580c"
                                ],
                                "#f97316" # Naranja por defecto si falla el dato
                            ],
                            "fill-extrusion-height": [
                                "coalesce",
                                [
                                    "interpolate", ["linear"], ["get", "numero_pisos"],
                                    0, 3.5, 1, 4, 2, 8, 5, 20
                                ],
                                4 # 4 metros por defecto si falla el dato
                            ],
                            "fill-extrusion-base": 0,
                            "fill-extrusion-opacity": 0.85
                        }
                    })

            # Step 3: Labels
            for row in layers:
                layer_id = row[0]
                if any(x in layer_id.lower() for x in ["nomencl", "etiqueta", "label", "u_nomen"]):
                    style["layers"].append({
                        "id": f"{layer_id}-label",
                        "type": "symbol",
                        "source": layer_id,
                        "source-layer": layer_id,
                        "minzoom": 14,
                        "layout": {
                            "text-field": ["coalesce", ["get", "TEXTO"], ["get", "texto"], ["get", "nombre"], ["get", "ETIQUETA"]],
                            "text-font": ["Open Sans Regular"],
                            "text-size": 12,
                            "text-allow-overlap": False,
                            "text-letter-spacing": 0.05
                        },
                        "paint": {
                            "text-color": "#ffffff",
                            "text-halo-color": "#0f172a",
                            "text-halo-width": 2
                        }
                    })
            return style
    except Exception as e:
        print(f"Style Generation Error: {e}")
        return style

@app.post("/api/v1/import/scan")
async def scan_data():
    import geopandas as gpd
    import fiona
    engine = create_engine(SYNC_DB_URL)
    results = []
    
    if not os.path.exists("data"): return {"status": "error", "message": "No data folder"}

    for filename in os.listdir("data"):
        path = os.path.join("data", filename)
        ext = os.path.splitext(filename)[1].lower()
        try:
            if ext == '.gpkg':
                for layer in fiona.listlayers(path):
                    gdf = gpd.read_file(path, layer=layer)
                    if process_gdf(gdf, layer, engine): results.append(layer)
            elif ext in ['.geojson', '.json']:
                gdf = gpd.read_file(path)
                if process_gdf(gdf, os.path.splitext(filename)[0], engine): results.append(filename)
        except Exception as e:
            print(f"Error scanning {filename}: {e}")
            
    return {"status": "finished", "imported": results}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
