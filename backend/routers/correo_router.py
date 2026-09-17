"""
routers/correo_router.py

Endpoints del módulo "Correo de Aprendices": cruza documento (columna D
del consolidado) contra documento/correo (columnas B/F de varios xls).
"""

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from database import guardar_ejecucion, listar_ejecuciones
from modules import correo_aprendices
from modules.file_utils import es_excel

router = APIRouter(prefix="/api/correos", tags=["Correo de Aprendices"])

STORAGE_ROOT = Path(__file__).resolve().parent.parent / "storage"
UPLOADS_DIR = STORAGE_ROOT / "uploads"
OUTPUTS_DIR = STORAGE_ROOT / "outputs"


@router.post("")
async def ejecutar_correos(
    fila_inicio_consolidado: int = Form(2),
    col_documento_consolidado: int = Form(4),
    fila_inicio_xls: int = Form(2),
    consolidado: UploadFile = File(...),
    archivos_xls: list[UploadFile] = File(...),
):
    if not es_excel(consolidado.filename):
        raise HTTPException(status_code=400, detail="El consolidado debe ser .xls, .xlsx o .xlsm.")
    if not archivos_xls:
        raise HTTPException(status_code=400, detail="Debes subir al menos un archivo xls con los correos.")

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_entrada = UPLOADS_DIR / id_ejecucion
    carpeta_salida = OUTPUTS_DIR / id_ejecucion
    carpeta_entrada.mkdir(parents=True, exist_ok=True)

    ruta_consolidado = carpeta_entrada / consolidado.filename
    with ruta_consolidado.open("wb") as f:
        shutil.copyfileobj(consolidado.file, f)

    rutas_xls = []
    for archivo in archivos_xls:
        if not es_excel(archivo.filename):
            continue
        destino = carpeta_entrada / archivo.filename
        with destino.open("wb") as f:
            shutil.copyfileobj(archivo.file, f)
        rutas_xls.append(destino)

    if not rutas_xls:
        raise HTTPException(status_code=400, detail="Ninguno de los archivos xls subidos es válido.")

    try:
        resultado = correo_aprendices.enriquecer_con_correos(
            consolidado=ruta_consolidado,
            archivos_xls=rutas_xls,
            salida_dir=carpeta_salida,
            fila_inicio_consolidado=fila_inicio_consolidado,
            col_documento_consolidado=col_documento_consolidado,
            fila_inicio_xls=fila_inicio_xls,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando los archivos: {e}")

    parametros = {
        "consolidado": consolidado.filename,
        "n_archivos_xls": len(rutas_xls),
        "fila_inicio_consolidado": fila_inicio_consolidado,
        "col_documento_consolidado": col_documento_consolidado,
        "fila_inicio_xls": fila_inicio_xls,
    }
    id_bd = guardar_ejecucion(
        modulo="correo_aprendices",
        parametros=parametros,
        resultado=resultado,
        archivos_generados={"CON_CORREOS": resultado["archivo_generado"]},
    )

    return {"id_ejecucion": id_ejecucion, "id_bd": id_bd, **resultado}


@router.get("/descargar/{id_ejecucion}/{nombre_archivo}")
async def descargar_resultado(id_ejecucion: str, nombre_archivo: str):
    ruta = OUTPUTS_DIR / id_ejecucion / nombre_archivo
    if not ruta.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return FileResponse(ruta, filename=nombre_archivo)


@router.get("/historial")
async def historial(limite: int = 20):
    return listar_ejecuciones(modulo="correo_aprendices", limite=limite)