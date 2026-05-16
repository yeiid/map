"""
NeuralJira Map Engine — Módulo de Ingesta v4 (ogr2ogr)
======================================================
Usa ogr2ogr (GDAL nativo en C) para la carga pesada de datos geoespaciales.
Mantiene hash-checking para importación incremental inteligente.

Ventajas sobre la versión Python pura:
  - PG_USE_COPY: protocolo binario de PostgreSQL (4-8x más rápido)
  - Transformación CRS nativa vía PROJ (sin pasar por Python)
  - -makevalid: validación de geometrías en C
  - Streaming: no carga todo el archivo en RAM
"""

import os
import json
import hashlib
import logging
import subprocess
import re
import psycopg2
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Callable

import fiona

logger = logging.getLogger("ingest")
HASH_STORE = "data/layer_hashes.json"


# ─── Utilidades ──────────────────────────────────────────────────────────────

def _file_hash(filepath: str) -> str:
    """Calcula MD5 del archivo para detectar cambios."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _load_hashes() -> dict:
    try:
        with open(HASH_STORE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_hashes(hashes: dict):
    os.makedirs(os.path.dirname(HASH_STORE), exist_ok=True)
    with open(HASH_STORE, "w") as f:
        json.dump(hashes, f, indent=2)


def _sanitize_table_name(name: str) -> str:
    """Normaliza el nombre de capa a un nombre de tabla PostgreSQL válido."""
    clean = name.lower().replace(" ", "_").replace("-", "_")
    # Eliminar caracteres no alfanuméricos excepto _
    clean = re.sub(r'[^a-z0-9_]', '', clean)
    return clean


def _get_db_conn(db_url: str):
    """Crea una conexión psycopg2 desde una URL de SQLAlchemy/PostgreSQL."""
    url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    return psycopg2.connect(url)


def _db_url_to_pg_string(db_url: str) -> str:
    """
    Convierte una URL estilo SQLAlchemy a un connection string de ogr2ogr.
    postgresql://user:pass@host:port/dbname → PG:host=... dbname=... user=... password=...
    """
    url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    parsed = urlparse(url)
    parts = [
        f"host={parsed.hostname or 'localhost'}",
        f"port={parsed.port or 5432}",
        f"dbname={parsed.path.lstrip('/')}",
        f"user={parsed.username or 'mapengine'}",
    ]
    if parsed.password:
        parts.append(f"password={parsed.password}")
    return "PG:" + " ".join(parts)


def table_exists(cur, table_name: str) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = %s AND table_schema = 'public')",
        (table_name,)
    )
    return cur.fetchone()[0]


def _get_row_count(cur, table_name: str) -> int:
    """Obtiene el conteo real de filas de una tabla."""
    try:
        cur.execute(f"SELECT count(*) FROM {table_name}")
        return cur.fetchone()[0]
    except Exception:
        return 0


def _setup_audit_trigger(cur, table_name: str):
    """Crea un trigger para rastrear la fecha de última edición (útil para QGIS)."""
    try:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS ultima_edicion TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        cur.execute("""
            CREATE OR REPLACE FUNCTION update_ultima_edicion()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.ultima_edicion = CURRENT_TIMESTAMP;
                RETURN NEW;
            END;
            $$ language 'plpgsql';
        """)
        trigger_name = f"trg_audit_{table_name}"
        cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
        cur.execute(f"""
            CREATE TRIGGER {trigger_name}
            BEFORE UPDATE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION update_ultima_edicion();
        """)
    except Exception as e:
        logger.warning(f"No se pudo crear el trigger de auditoría en {table_name}: {e}")


# ─── Motor Principal: ogr2ogr ───────────────────────────────────────────────

def _run_ogr2ogr(
    filepath: str,
    layer_name: str,
    target_table: str,
    pg_conn_str: str,
    mode: str = "replace",
) -> subprocess.CompletedProcess:
    """
    Ejecuta ogr2ogr para cargar una capa en PostGIS.
    
    Usa:
      - PG_USE_COPY YES: protocolo COPY binario (máxima velocidad)
      - -t_srs EPSG:4326: reproyección nativa en C vía PROJ
      - -makevalid: validación de geometrías sin Python
      - -nlt PROMOTE_TO_MULTI: evita errores por geometrías mixtas
      - -lco GEOMETRY_NAME=geometry: nombre consistente de columna geom
    """
    cmd = [
        "ogr2ogr",
        "-f", "PostgreSQL",
        pg_conn_str,
        filepath,
        layer_name,
        "-nln", target_table,           # Nombre de tabla destino
        "-t_srs", "EPSG:4326",          # Reproyectar a WGS84
        "-makevalid",                    # Corregir geometrías inválidas
        "-nlt", "PROMOTE_TO_MULTI",      # Promover a Multi para consistencia
        "-lco", "GEOMETRY_NAME=geometry", # Nombre estándar de columna geom
        "-lco", "FID=id",               # Nombre de la columna ID
        "-lco", "SPATIAL_INDEX=GIST",   # Crear índice GIST automáticamente
        "--config", "PG_USE_COPY", "YES", # Usar protocolo COPY (4x más rápido)
        "--config", "OGR_TRUNCATE", "NO",
        "-progress",                     # Mostrar progreso
    ]

    if mode == "replace":
        cmd.append("-overwrite")          # DROP + CREATE
    elif mode == "append":
        cmd.append("-append")             # INSERT INTO existente

    logger.info(f"🔧 ogr2ogr: {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,  # Timeout de 1 hora para archivos enormes
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip() or "Error desconocido en ogr2ogr"
        logger.error(f"❌ ogr2ogr falló para {layer_name}: {error_msg}")
        raise RuntimeError(f"ogr2ogr error: {error_msg}")

    # Loguear cualquier warning
    if result.stderr:
        for line in result.stderr.strip().split("\n"):
            if line.strip():
                logger.info(f"  ogr2ogr: {line.strip()}")

    return result


def ingest_layer_ogr2ogr(
    filepath: str,
    layer_name: str,
    db_url: str,
    progress_cb: Optional[Callable] = None,
    force: bool = False,
    mode: str = "replace",
) -> dict:
    """
    Ingesta una capa geoespacial usando ogr2ogr (GDAL nativo).
    
    Pipeline:
      1. Hash-check → si no cambió, skip
      2. ogr2ogr carga en tabla staging (o directa si append)
      3. Swap atómico: staging → tabla final
      4. Post-procesamiento: ANALYZE + triggers de auditoría
    """
    table_name = _sanitize_table_name(layer_name or Path(filepath).stem)
    file_hash = _file_hash(filepath)
    hash_key = f"{Path(filepath).name}::{layer_name}"
    pg_conn_str = _db_url_to_pg_string(db_url)

    conn = _get_db_conn(db_url)
    try:
        with conn.cursor() as cur:
            exists = table_exists(cur, table_name)
            hashes = _load_hashes()

            # Skip si no hay cambios
            if not force and exists and hashes.get(hash_key) == file_hash:
                logger.info(f"✔ Capa {layer_name} ya está al día. Saltando.")
                return {"status": "skipped", "layer": layer_name, "table": table_name, "rows": 0}

            logger.info(f"🚀 Importando capa: {layer_name} → {table_name} (Modo: {mode})")

            if progress_cb:
                progress_cb(0, 100, f"Cargando {layer_name} con ogr2ogr...")

            if mode == "replace":
                # Estrategia: cargar en tabla staging, luego swap atómico
                staging_table = f"{table_name}_staging"

                # Limpiar staging anterior si existe
                cur.execute(f"DROP TABLE IF EXISTS {staging_table} CASCADE")
                conn.commit()

                # ogr2ogr carga directamente en la staging table
                _run_ogr2ogr(filepath, layer_name, staging_table, pg_conn_str, mode="replace")

                if progress_cb:
                    progress_cb(70, 100, f"Swap atómico {layer_name}...")

                # Verificar que la staging tiene datos
                row_count = _get_row_count(cur, staging_table)
                if row_count == 0:
                    cur.execute(f"DROP TABLE IF EXISTS {staging_table} CASCADE")
                    conn.commit()
                    return {"status": "empty", "layer": layer_name, "table": table_name, "rows": 0}

                # Swap atómico: staging → final
                cur.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
                cur.execute(f"ALTER TABLE {staging_table} RENAME TO {table_name}")

                # Renombrar índices si existen
                try:
                    cur.execute(f"ALTER INDEX IF EXISTS {staging_table}_geometry_geom_idx RENAME TO idx_{table_name}_geom")
                except Exception:
                    conn.rollback()
                    # Crear índice GIST si ogr2ogr no lo creó
                    try:
                        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_geom ON {table_name} USING GIST (geometry)")
                    except Exception:
                        pass

                conn.commit()

            elif mode == "append":
                _run_ogr2ogr(filepath, layer_name, table_name, pg_conn_str, mode="append")
                row_count = _get_row_count(cur, table_name)

            if progress_cb:
                progress_cb(90, 100, f"Optimizando {layer_name}...")

            # Post-procesamiento
            _setup_audit_trigger(cur, table_name)
            cur.execute(f"ANALYZE {table_name}")
            conn.commit()

            # Obtener conteo final
            row_count = _get_row_count(cur, table_name)

            # Guardar hash solo si fue exitoso
            hashes[hash_key] = file_hash
            _save_hashes(hashes)

            if progress_cb:
                progress_cb(100, 100, f"✅ {layer_name}: {row_count} features")

            logger.info(f"✅ Capa {layer_name} importada: {row_count} filas en {table_name}")

    finally:
        conn.close()

    return {"status": "imported", "layer": layer_name, "table": table_name, "rows": row_count}


# ─── DXF: se mantiene Python porque son archivos pequeños ────────────────────

def _ingest_dxf(filepath, db_url, progress_cb=None, force=False, mode="replace"):
    """
    Importa un archivo DXF agrupando features por el campo 'Layer'.
    Los DXF son típicamente pequeños (<50MB), así que Python es suficiente.
    """
    import collections
    from shapely.geometry import shape
    from shapely.validation import make_valid
    from psycopg2.extras import execute_values

    CHUNK_SIZE = 5000
    logger.info(f"📐 Procesando DXF: {filepath}")

    groups = collections.defaultdict(list)
    total_features = 0
    with fiona.open(filepath, layer="entities") as src:
        for feat in src:
            layer_name = feat.get("properties", {}).get("Layer", "default")
            if not feat.get("geometry"):
                continue
            groups[layer_name].append(feat)
            total_features += 1

    logger.info(f"📦 DXF: {total_features} features en {len(groups)} capas: {list(groups.keys())}")

    results = []
    for layer_name, features in groups.items():
        if progress_cb:
            progress_cb(layer_name, 0, len(features), f"Procesando {layer_name}...")

        table_name = _sanitize_table_name(f"dxf_{layer_name}")
        file_hash = hashlib.md5(f"{Path(filepath).name}::{layer_name}".encode()).hexdigest()
        hash_key = f"{Path(filepath).name}::{layer_name}"

        conn = _get_db_conn(db_url)
        try:
            with conn.cursor() as cur:
                exists = table_exists(cur, table_name)
                hashes = _load_hashes()

                if not force and exists and hashes.get(hash_key) == file_hash:
                    logger.info(f"✔ Capa DXF {layer_name} ya está al día. Saltando.")
                    results.append({"status": "skipped", "layer": layer_name, "table": table_name, "rows": 0})
                    continue

                # Inferir esquema desde la primera feature
                first = features[0]
                schema_props = first.get("properties", {})
                prop_keys = []
                col_defs = []
                type_map = {
                    "int": "INTEGER", "float": "DOUBLE PRECISION", "str": "TEXT",
                    "date": "DATE", "datetime": "TIMESTAMP", "bool": "BOOLEAN",
                }

                for k, v in schema_props.items():
                    if v is None:
                        continue
                    ck = k.lower().replace(" ", "_").replace("-", "_")
                    if ck in ("id", "geometry", "order", "table"):
                        ck = f"attr_{ck}"
                    sql_t = type_map.get(type(v).__name__, "TEXT")
                    col_defs.append(f'"{ck}" {sql_t}')
                    prop_keys.append((k, ck))

                target_table = table_name
                if mode == "replace":
                    target_table = f"{table_name}_staging"
                    cur.execute(f"DROP TABLE IF EXISTS {target_table} CASCADE")
                    cur.execute(f"CREATE TABLE {target_table} (id SERIAL PRIMARY KEY, {','.join(col_defs)}, geometry geometry(Geometry, 4326))")
                elif mode == "append":
                    if not exists:
                        cur.execute(f"CREATE TABLE {target_table} (id SERIAL PRIMARY KEY, {','.join(col_defs)}, geometry geometry(Geometry, 4326))")
                        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{target_table}_geom ON {target_table} USING GIST (geometry)")

                cols = ", ".join(f'"{ck}"' for _, ck in prop_keys) + ", geometry"
                tpl = "(" + ",".join(["%s"] * len(prop_keys)) + ", ST_GeomFromWKB(%s, 4326))"

                rows = []
                count = 0
                for feat in features:
                    if not feat.get("geometry"):
                        continue
                    try:
                        geom = shape(feat["geometry"])
                        if not geom.is_valid:
                            geom = make_valid(geom)
                        wkb = geom.wkb
                    except Exception:
                        continue

                    p = feat.get("properties", {})
                    row = [p.get(k) if p.get(k) != "" else None for k, _ in prop_keys]
                    row.append(psycopg2.Binary(wkb))
                    rows.append(tuple(row))
                    count += 1

                    if len(rows) >= CHUNK_SIZE:
                        execute_values(cur, f"INSERT INTO {target_table} ({cols}) VALUES %s", rows, template=tpl)
                        rows = []
                        conn.commit()
                        if progress_cb:
                            progress_cb(layer_name, count, len(features), f"Cargando {layer_name}...")

                if rows:
                    execute_values(cur, f"INSERT INTO {target_table} ({cols}) VALUES %s", rows, template=tpl)
                    conn.commit()

                if mode == "replace":
                    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{target_table}_geom ON {target_table} USING GIST (geometry)")
                    cur.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
                    cur.execute(f"ALTER TABLE {target_table} RENAME TO {table_name}")
                    cur.execute(f"ALTER INDEX idx_{target_table}_geom RENAME TO idx_{table_name}_geom")

                _setup_audit_trigger(cur, table_name)
                cur.execute(f"ANALYZE {table_name}")
                conn.commit()

                hashes[hash_key] = file_hash
                _save_hashes(hashes)

                results.append({"status": "imported", "layer": layer_name, "table": table_name, "rows": count})

                if progress_cb:
                    progress_cb(layer_name, count, len(features), f"✅ {layer_name}: {count} features")
        finally:
            conn.close()

    return {"status": "finished", "results": results}


# ─── Punto de Entrada Principal ──────────────────────────────────────────────

def ingest_file(filepath, db_url, progress_cb=None, force=False, mode="replace"):
    """
    Detecta el tipo de archivo y usa el motor apropiado:
      - GPKG/GeoJSON → ogr2ogr (GDAL nativo, máximo rendimiento)
      - DXF → Python (archivos pequeños, agrupación por Layer)
    """
    ext = Path(filepath).suffix.lower()

    if ext == ".gpkg":
        layers = fiona.listlayers(filepath)
        results = []
        total_layers = len([l for l in layers if l != "layer_styles"])
        
        for i, lyr in enumerate(layers):
            if lyr == "layer_styles":
                continue
            
            logger.info(f"📦 Capa {i+1}/{total_layers}: {lyr}")
            
            def layer_progress(current, total, msg):
                if progress_cb:
                    # Combinar progreso de capa con progreso global
                    global_pct = int(((i + current / max(total, 1)) / max(total_layers, 1)) * 100)
                    progress_cb(lyr, global_pct, 100, msg)
            
            try:
                res = ingest_layer_ogr2ogr(
                    filepath, lyr, db_url,
                    progress_cb=layer_progress,
                    force=force, mode=mode,
                )
                results.append(res)
            except Exception as e:
                logger.error(f"❌ Error en capa {lyr}: {e}")
                results.append({"status": "error", "layer": lyr, "error": str(e), "rows": 0})

        return {"status": "finished", "results": results}

    elif ext == ".dxf":
        return _ingest_dxf(filepath, db_url, progress_cb, force, mode)

    else:
        # GeoJSON u otros formatos soportados por GDAL
        name = Path(filepath).stem
        
        def file_progress(current, total, msg):
            if progress_cb:
                progress_cb(name, current, total, msg)
        
        try:
            res = ingest_layer_ogr2ogr(
                filepath, name, db_url,
                progress_cb=file_progress,
                force=force, mode=mode,
            )
        except Exception as e:
            logger.error(f"❌ Error importando {name}: {e}")
            res = {"status": "error", "layer": name, "error": str(e), "rows": 0}
        
        return {"status": "finished", "results": [res]}
