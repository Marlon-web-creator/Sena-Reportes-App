"""
routers/no_programados_router.py

Endpoints del módulo "Verificador No Programados". El procesamiento corre
en un hilo aparte: el POST devuelve de inmediato un id_ejecucion, y el
frontend consulta /progreso/{id} cada cierto tiempo.

Entrada/intermedios (excel + PDFs) en carpeta temporal. Como el
procesamiento sigue corriendo en segundo plano después de responder el
POST, la carpeta temporal se borra DENTRO del hilo (en su finally), no
en el endpoint. El archivo de RESULTADO se sube a Supabase Storage antes
de marcar la ejecución como terminada.
"""

import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

import progreso
import supabase_storage
from database import guardar_ejecucion, listar_ejecuciones
from modules import no_programados

router = APIRouter(prefix="/api/no-programados", tags=["No Programados"])

NOMBRE_MODULO = "no_programados"


def _ruta_segura(base: Path, nombre_relativo: str) -> Path:
    """Evita path traversal: normaliza y rechaza rutas que se salgan de `base`."""
    limpio = nombre_relativo.replace("\\", "/").lstrip("/")
    destino = (base / limpio).resolve()
    base_resuelta = base.resolve()
    if base_resuelta != destino and base_resuelta not in destino.parents:
        raise ValueError(f"Ruta de archivo no válida: {nombre_relativo}")
    return destino


def _subir_generado(ruta_local, id_ejecucion: str) -> str:
    """
    Sube el archivo resultado (hoy en la carpeta temporal local) a
    Supabase Storage y devuelve el path dentro del bucket.
    """
    ruta_local = Path(ruta_local)
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{ruta_local.name}"
    supabase_storage.subir_bytes(ruta_storage, ruta_local.read_bytes())
    return ruta_storage


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
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"no_programados_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_bd = carpeta_entrada / "BD"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_bd.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
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
    except HTTPException:
        # El hilo nunca arranca, así que la limpieza es responsabilidad
        # de este endpoint.
        shutil.rmtree(carpeta_temporal, ignore_errors=True)
        raise

    progreso.iniciar(id_ejecucion)

    def _tarea():
        try:
            def callback(mensaje, actual, total):
                progreso.actualizar(id_ejecucion, mensaje, actual, total)

            try:
                resultado = no_programados.procesar(
                    archivo_excel=ruta_excel,
                    carpeta_bd=carpeta_bd,
                    salida_dir=carpeta_salida,
                    progress_callback=callback,
                )

                # Subir el resultado a Supabase Storage ANTES de guardar
                # en BD y de avisar al frontend que ya terminó.
                ruta_storage = _subir_generado(resultado["archivo_generado"], id_ejecucion)
                resultado["archivo_generado"] = ruta_storage

                guardar_ejecucion(
                    modulo=NOMBRE_MODULO,
                    parametros={"excel": excel.filename, "n_pdfs": pdfs_guardados},
                    resultado=resultado,
                    archivos_generados={"PROCESADO": ruta_storage},
                )
                progreso.finalizar_ok(id_ejecucion, resultado)
            except Exception as e:
                progreso.finalizar_error(id_ejecucion, str(e))
        finally:
            # Aquí sí se puede borrar: el hilo ya terminó de usar la
            # carpeta temporal (subió lo que necesitaba a Storage).
            shutil.rmtree(carpeta_temporal, ignore_errors=True)

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
    """
    Redirige a una URL firmada temporal de Supabase Storage.
    Asume la convención de rutas usada en _subir_generado():
    generados/<modulo>/<id_ejecucion>/<nombre_archivo>
    """
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{nombre_archivo}"

    if not supabase_storage.existe_archivo(ruta_storage):
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    url = supabase_storage.crear_url_firmada(ruta_storage, nombre_descarga=nombre_archivo)

    if not url:
        raise HTTPException(status_code=500, detail="No se pudo generar el enlace de descarga.")

    return RedirectResponse(url)


@router.get("/historial")
async def historial(limite: int = 20):
    return listar_ejecuciones(modulo=NOMBRE_MODULO, limite=limite)