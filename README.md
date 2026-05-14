# 🌐 NeuralJira Map Engine

Sistema independiente de mapas (Map-as-a-Service) diseñado para ser desplegado en Dokploy y consumido por múltiples proyectos.

## 🚀 Componentes

- **PostGIS**: Almacenamiento de datos geoespaciales.
- **Martin**: Servidor de Vector Tiles (MVT) ultrarrápido que lee directamente de PostGIS.
- **Map Manager (FastAPI)**: Gestión de estilos, fuentes, sprites e ingesta de datos.

## 🛠️ Instalación en Dokploy

1. Crea un nuevo **Project** en Dokploy.
2. Añade un **Compose** service.
3. Conecta este repositorio o sube los archivos de `/home/yeiid/Escritorio/map`.
4. Configura las variables de entorno (Opcional, tiene valores por defecto).

## 📂 Estructura de Carpetas

- `/styles`: Archivos JSON de estilos (MapBox/MapLibre compatible).
- `/fonts`: Glifos en formato PBF (puedes usar los de `openmaptiles`).
- `/data`: Almacenamiento local para archivos GeoJSON o MBTiles.

## 🗺️ Cómo usar el Mapa

En tu aplicación frontend (Astro, React, etc.), usa la URL del estilo:

```javascript
const map = new maplibregl.Map({
    container: 'map',
    style: 'http://<tu-dominio-manager>/styles/basic.json',
    center: [-72.92, 11.54], // Riohacha, La Guajira
    zoom: 15
});
```

## 📥 Ingesta de Datos (QGIS)

Para ver tus datos de QGIS aquí:
1. Abre QGIS.
2. Conéctate a la base de datos PostGIS usando las credenciales en `docker-compose.yml`.
3. Arrastra tus capas a la base de datos.
4. Martin detectará automáticamente las tablas y las servirá en `http://<tu-dominio-tiles>/<nombre_tabla>`.
