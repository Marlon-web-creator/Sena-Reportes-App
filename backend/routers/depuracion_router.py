"""
routers/depuracion_router.py

Endpoints del módulo "Depuración de Por Evaluar", equivalente web de
Depuracion.py.
"""

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from database import guardar_ejecucion, listar_ejecuciones
from modules import depuracion

router = APIRouter(prefix="/api/depuracion", tags=["Depuración"])

STORAGE_ROOT = Path(__file__).resolve().parent.parent / "storage"
UPLOADS_DIR = STORAGE_ROOT / "uploads"
OUTPUTS_DIR = STORAGE_ROOT / "outputs"

EXTENSIONES_PERMITIDAS = {".xlsx"}


@router.post("")
async def ejecutar_depuracion(
    limite: int = Form(20),
    archivo: UploadFile = File(...),
):
    extension = Path(archivo.filename).suffix.lower()
    if extension not in EXTENSIONES_PERMITIDAS:
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx.")
    if limite < 0:
        raise HTTPException(status_code=400, detail="El límite debe ser un número entero positivo.")

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_entrada = UPLOADS_DIR / id_ejecucion
    carpeta_salida = OUTPUTS_DIR / id_ejecucion
    carpeta_entrada.mkdir(parents=True, exist_ok=True)

    ruta_entrada = carpeta_entrada / archivo.filename
    with ruta_entrada.open("wb") as f:
        shutil.copyfileobj(archivo.file, f)

    try:
        resultado = depuracion.depurar(
            archivo=ruta_entrada,
            limite=limite,
            salida_dir=carpeta_salida,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando el archivo: {e}")

    parametros = {"limite": limite, "archivo": archivo.filename}
    id_bd = guardar_ejecucion(
        modulo="depuracion",
        parametros=parametros,
        resultado=resultado,
        archivos_generados={"PROCESADO": resultado["archivo_generado"]},
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
    return listar_ejecuciones(modulo="depuracion", limite=limite)