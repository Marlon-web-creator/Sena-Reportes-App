"""
main.py

Punto de entrada de la API. Ejecutar con:
    py -m uvicorn main:app --reload --port 8000

Luego abrir http://127.0.0.1:8000 en el navegador.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from database import init_db
from auth.router import router as router_auth
from routers import (
    consolidador_router,
    depuracion_router,
    no_programados_router,
    correo_router,
    archivos_router,
)

app = FastAPI(title="SENA Reportes App", version="0.1.0")

init_db()

# Login / logout / me — endpoints públicos
app.include_router(router_auth)

# Resto de routers (sin protección global: cada endpoint decide)
app.include_router(consolidador_router.router)
app.include_router(depuracion_router.router)
app.include_router(no_programados_router.router)
app.include_router(correo_router.router)
app.include_router(archivos_router.router)

print(">>> RUTAS ARCHIVOS REGISTRADAS:")
for ruta in app.routes:
    if "/api/archivos" in ruta.path:
        print(ruta.path, sorted(ruta.methods or []))

print(">>> RUTAS AUTH REGISTRADAS:")
for ruta in app.routes:
    if "/api/auth" in ruta.path:
        print(ruta.path, sorted(ruta.methods or []))

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")