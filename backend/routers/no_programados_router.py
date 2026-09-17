"""
routers/no_programados_router.py

Endpoints del módulo "Verificador No Programados". El procesamiento corre
en un hilo aparte: el POST devuelve de inmediato un id_ejecucion, y el
frontend consulta /progreso/{id} cada cierto tiempo.
"""

import shutil
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

import progreso
from database import guardar_ejecucion, listar_ejecuciones
from modules import no_programados

router = APIRouter(prefix="/api/no-programados", tags=["No Programados"])

STORAGE_ROOT = Path(__file__).resolve().parent.parent / "storage"
UPLOADS_DIR = STORAGE_ROOT / "uploads"
OUTPUTS_DIR = STORAGE_ROOT / "outputs"


def _ruta_segura(base: Path, nombre_relativo: str) -> Path:
    """Evita path traversal: normaliza y rechaza rutas que se salgan de `base`."""
    limpio = nombre_relativo.replace("\\", "/").lstrip("/")
    destino = (base / limpio).resolve()
    base_resuelta = base.resolve()
    if base_resuelta != destino and base_resuelta not in destino.parents:
        raise ValueError(f"Ruta de archivo no válida: {nombre_relativo}")
    return destino


@router.post("")
async def iniciar_procesamiento(
    excel: UploadFile = File(...),
    pdfs: list[UploadFile] = File(...),
):
    if not excel.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="El consolidado debe ser .xlsx o .xlsm.")
    if not pdfs:
        raise HTTPException(status_code=400, detail="Debes subir al menos un PDF.")

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_entrada = UPLOADS_DIR / id_ejecucion
    carpeta_bd = carpeta_entrada / "BD"
    carpeta_salida = OUTPUTS_DIR / id_ejecucion
    carpeta_bd.mkdir(parents=True, exist_ok=True)

    ruta_excel = carpeta_entrada / excel.filename
    with ruta_excel.open("wb") as f:
        shutil.copyfileobj(excel.file, f)

    pdfs_guardados = 0
    for pdf in pdfs:
        if not pdf.filename.lower().endswith(".pdf"):
            continue
        try:
            destino = _ruta_segura(carpeta_bd, pdf.filename)
        except ValueError:
            continue
        destino.parent.mkdir(parents=True, exist_ok=True)
        with destino.open("wb") as f:
            shutil.copyfileobj(pdf.file, f)
        pdfs_guardados += 1

    if pdfs_guardados == 0:
        raise HTTPException(status_code=400, detail="Ninguno de los archivos subidos es un .pdf válido.")

    progreso.iniciar(id_ejecucion)

    def _tarea():
        def callback(mensaje, actual, total):
            progreso.actualizar(id_ejecucion, mensaje, actual, total)

        try:
            resultado = no_programados.procesar(
                archivo_excel=ruta_excel,
                carpeta_bd=carpeta_bd,
                salida_dir=carpeta_salida,
                progress_callback=callback,
            )
            guardar_ejecucion(
                modulo="no_programados",
                parametros={"excel": excel.filename, "n_pdfs": pdfs_guardados},
                resultado=resultado,
                archivos_generados={"PROCESADO": resultado["archivo_generado"]},
            )
            progreso.finalizar_ok(id_ejecucion, resultado)
        except Exception as e:
            progreso.finalizar_error(id_ejecucion, str(e))

    threading.Thread(target=_tarea, daemon=True).start()

    return {"id_ejecucion": id_ejecucion}


@router.get("/progreso/{id_ejecucion}")
async def consultar_progreso(id_ejecucion: str):
    estado = progreso.consultar(id_ejecucion)
    if estado is None:
        raise HTTPException(status_code=404, detail="Ejecución no encontrada.")
    return estado


@router.get("/descargar/{id_ejecucion}/{nombre_archivo}")
async def descargar_resultado(id_ejecucion: str, nombre_archivo: str):
    ruta = OUTPUTS_DIR / id_ejecucion / nombre_archivo
    if not ruta.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return FileResponse(ruta, filename=nombre_archivo)


@router.get("/historial")
async def historial(limite: int = 20):
    return listar_ejecuciones(modulo="no_programados", limite=limite)