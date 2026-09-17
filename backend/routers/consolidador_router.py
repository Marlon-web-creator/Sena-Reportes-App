"""
routers/consolidador_router.py

Endpoints del módulo "Consolidador NoAprobados / PorEvaluar", equivalente
web del script NoAprobados.py.
"""

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from database import guardar_ejecucion, listar_ejecuciones
from modules import no_aprobados

router = APIRouter(prefix="/api/consolidador", tags=["Consolidador NoAprobados"])

STORAGE_ROOT = Path(__file__).resolve().parent.parent / "storage"
UPLOADS_DIR = STORAGE_ROOT / "uploads"
OUTPUTS_DIR = STORAGE_ROOT / "outputs"

EXTENSIONES_PERMITIDAS = {".xls", ".xlsx"}


@router.post("")
async def ejecutar_consolidador(
    filtro: str = Form(...),               # "NO APROBADO" o "POR EVALUAR"
    generar_general: bool = Form(False),
    archivos: list[UploadFile] = File(...),
):
    if filtro not in no_aprobados.FILTROS:
        raise HTTPException(status_code=400, detail=f"Filtro no válido: {filtro}")
    if not archivos:
        raise HTTPException(status_code=400, detail="Debes subir al menos un archivo.")

    # Carpeta única por ejecución, para no mezclar archivos entre corridas
    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_entrada = UPLOADS_DIR / id_ejecucion
    carpeta_salida = OUTPUTS_DIR / id_ejecucion
    carpeta_entrada.mkdir(parents=True, exist_ok=True)

    rutas_guardadas = []
    for archivo in archivos:
        extension = Path(archivo.filename).suffix.lower()
        if extension not in EXTENSIONES_PERMITIDAS:
            continue  # se ignoran archivos que no sean .xls/.xlsx
        destino = carpeta_entrada / archivo.filename
        with destino.open("wb") as f:
            shutil.copyfileobj(archivo.file, f)
        rutas_guardadas.append(destino)

    if not rutas_guardadas:
        raise HTTPException(status_code=400, detail="Ninguno de los archivos subidos es .xls o .xlsx.")

    try:
        resultado = no_aprobados.consolidar(
            archivos=rutas_guardadas,
            valor_filtro=filtro,
            generar_general=generar_general,
            salida_dir=carpeta_salida,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando los archivos: {e}")

    parametros = {"filtro": filtro, "generar_general": generar_general, "n_archivos": len(rutas_guardadas)}
    id_bd = guardar_ejecucion(
        modulo="consolidador_no_aprobados",
        parametros=parametros,
        resultado=resultado["stats"],
        archivos_generados=resultado["archivos_generados"],
    )

    return {
        "id_ejecucion": id_ejecucion,
        "id_bd": id_bd,
        **resultado,
    }


@router.get("/descargar/{id_ejecucion}/{nombre_archivo}")
async def descargar_resultado(id_ejecucion: str, nombre_archivo: str):
    ruta = OUTPUTS_DIR / id_ejecucion / nombre_archivo
    if not ruta.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return FileResponse(ruta, filename=nombre_archivo)


@router.get("/historial")
async def historial(limite: int = 20):
    return listar_ejecuciones(modulo="consolidador_no_aprobados", limite=limite)
