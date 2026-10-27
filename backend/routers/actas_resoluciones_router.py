"""
routers/actas_resoluciones_router.py

Endpoints del módulo "Actas y Resoluciones". Igual que Horas Instructores y
No Programados: el POST devuelve de inmediato un id_ejecucion, el
procesamiento corre en un hilo aparte y el frontend consulta
/progreso/{id} cada cierto tiempo.

Los PDFs de actas y resoluciones NO se suben por formulario: se toman de la
sección "Base de Datos" filtrando por módulo "Actas y Resoluciones".
Cualquier variación de mayúsculas, espacios o guiones ("actas y resoluciones",
"ACTAS-Y-RESOLUCIONES", "actas_y_resoluciones") cuenta como el mismo módulo.
Solo se leen los archivos .pdf; el resto de archivos con ese módulo se ignora.

Lo que SÍ se sube en cada ejecución es el consolidado (campo "consolidado",
.xlsx/.xlsm): el módulo le agrega las columnas ESTADO REAL y
OBSERVACIÓN ACTAS/RESOLUCIONES.
"""

import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

import progreso
import supabase_storage
from database import (
    guardar_ejecucion,
    listar_archivos_por_modulo,
    listar_ejecuciones,
)
from modules import actas_resoluciones

logger = logging.getLogger("actas_resoluciones.router")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] ar.router: %(message)s", datefmt="%H:%M:%S",
    ))
    logger.addHandler(_h)
    logger.propagate = False

router = APIRouter(prefix="/api/actas-resoluciones", tags=["Actas y Resoluciones"])

NOMBRE_MODULO = "actas_resoluciones"

# Nombres de módulo que se aceptan en la BD (ya normalizados: minúsculas y
# con "_"; listar_archivos_por_modulo normaliza espacios y guiones).
MODULOS_ACEPTADOS = ("actas_y_resoluciones", "actas_resoluciones", "actas_y_resolucion")

MAX_DESCARGAS_PARALELAS = int(os.environ.get("AR_DESCARGAS_PARALELAS") or 8)
# Los PDFs pesan más que los Excels: margen más amplio por archivo.
TIMEOUT_DESCARGA_POR_ARCHIVO = float(os.environ.get("AR_TIMEOUT_DESCARGA") or 90)


def _subir_generado(ruta_local, id_ejecucion: str) -> str:
    """Sube el Excel resultado a Supabase Storage y devuelve su path en el bucket."""
    ruta_local = Path(ruta_local)
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{ruta_local.name}"
    supabase_storage.subir_bytes(ruta_storage, ruta_local.read_bytes())
    return ruta_storage


def _archivos_bd() -> tuple[list[dict], int]:
    """
    (PDFs de la BD con módulo 'Actas y Resoluciones' sin duplicados por id,
    cantidad de archivos de ese módulo que se ignoran por no ser PDF).
    """
    vistos: dict[int, dict] = {}
    for modulo in MODULOS_ACEPTADOS:
        for a in listar_archivos_por_modulo(modulo):
            vistos.setdefault(a["id"], a)
    pdfs, ignorados = [], 0
    for a in vistos.values():
        nombre = a["nombre_original"]
        if nombre.startswith("~$"):
            continue
        if nombre.lower().endswith(actas_resoluciones.EXTENSIONES_PDF):
            pdfs.append(a)
        else:
            ignorados += 1
    return pdfs, ignorados


@router.post("")
async def iniciar_procesamiento(consolidado: UploadFile = File(...)):
    logger.info("POST /api/actas-resoluciones — consolidado=%s", consolidado.filename)

    if not consolidado.filename or not consolidado.filename.lower().endswith(
        actas_resoluciones.EXTENSIONES_CONSOLIDADO
    ):
        raise HTTPException(
            status_code=400,
            detail="El consolidado debe ser un archivo Excel (.xlsx o .xlsm).",
        )

    archivos_pdf, ignorados_no_pdf = _archivos_bd()
    if not archivos_pdf:
        raise HTTPException(
            status_code=400,
            detail=(
                "No hay ningún PDF en la Base de Datos con el módulo "
                "'Actas y Resoluciones'. Súbelo primero desde esa sección."
            ),
        )

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"actas_resoluciones_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_actas = carpeta_entrada / "ACTAS"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_actas.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
        ruta_consolidado = carpeta_entrada / Path(consolidado.filename).name
        ruta_consolidado.write_bytes(await consolidado.read())

        # Cada archivo va en su propia subcarpeta (id) para que dos PDFs con el
        # mismo nombre en carpetas distintas de la BD no se pisen.
        def _descargar_uno(archivo: dict):
            nombre = archivo["nombre_original"]
            try:
                destino = carpeta_actas / str(archivo["id"]) / Path(nombre).name
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(supabase_storage.descargar_bytes(archivo["ruta"]))
                return nombre, True, None
            except Exception as e:
                return nombre, False, f"{type(e).__name__}: {e}"

        t0 = time.time()
        guardados = 0
        fallidos: list[str] = []

        pool = ThreadPoolExecutor(max_workers=MAX_DESCARGAS_PARALELAS, thread_name_prefix="ar-dl")
        futuros = {pool.submit(_descargar_uno, a): a["nombre_original"] for a in archivos_pdf}
        oleadas = -(-len(futuros) // MAX_DESCARGAS_PARALELAS)
        terminados = set()
        try:
            for fut in as_completed(futuros, timeout=TIMEOUT_DESCARGA_POR_ARCHIVO * oleadas + 10):
                terminados.add(fut)
                try:
                    nombre, ok, err = fut.result()
                except Exception:
                    logger.exception("Error inesperado descargando %s", futuros[fut])
                    fallidos.append(futuros[fut])
                    continue
                if ok:
                    guardados += 1
                else:
                    logger.warning("Fallo descargando %s: %s", nombre, err)
                    fallidos.append(nombre)
        except FuturesTimeout:
            for fut, nombre in futuros.items():
                if fut not in terminados:
                    logger.warning("Timeout de descarga: %s se omite", nombre)
                    fallidos.append(nombre)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        if guardados == 0:
            raise HTTPException(
                status_code=400,
                detail="No se pudo descargar ningún PDF desde la Base de Datos.",
            )
        logger.info("Descarga: %d ok, %d fallidos (%.1fs)", guardados, len(fallidos), time.time() - t0)
    except HTTPException:
        shutil.rmtree(carpeta_temporal, ignore_errors=True)
        raise
    except Exception:
        logger.exception("Error preparando ejecución %s", id_ejecucion)
        shutil.rmtree(carpeta_temporal, ignore_errors=True)
        raise

    progreso.iniciar(id_ejecucion)
    nombre_consolidado = consolidado.filename

    def _tarea():
        try:
            def callback(mensaje, actual, total):
                progreso.actualizar(id_ejecucion, mensaje, actual, total)

            try:
                resultado = actas_resoluciones.procesar(
                    archivo_consolidado=ruta_consolidado,
                    carpeta_actas=carpeta_actas,
                    salida_dir=carpeta_salida,
                    progress_callback=callback,
                )
                ruta_storage = _subir_generado(resultado["archivo_generado"], id_ejecucion)
                resultado["archivo_generado"] = ruta_storage
                resultado["pdfs_fallidos_al_descargar"] = fallidos
                resultado["archivos_no_pdf_ignorados"] = ignorados_no_pdf

                guardar_ejecucion(
                    modulo=NOMBRE_MODULO,
                    parametros={
                        "consolidado": nombre_consolidado,
                        "n_pdfs": guardados,
                    },
                    resultado=resultado,
                    archivos_generados={"CONSOLIDADO": ruta_storage},
                )
                progreso.finalizar_ok(id_ejecucion, resultado)
            except Exception as e:
                logger.exception("Hilo %s: error: %s", id_ejecucion, e)
                progreso.finalizar_error(id_ejecucion, str(e))
        finally:
            shutil.rmtree(carpeta_temporal, ignore_errors=True)

    threading.Thread(target=_tarea, daemon=True, name=f"ar-{id_ejecucion}").start()
    return {"id_ejecucion": id_ejecucion}


@router.get("/progreso/{id_ejecucion}")
async def consultar_progreso(id_ejecucion: str):
    estado = progreso.consultar(id_ejecucion)
    if estado is None:
        raise HTTPException(status_code=404, detail="Ejecución no encontrada.")
    return estado


@router.get("/descargar/{id_ejecucion}/{nombre_archivo}")
async def descargar_resultado(id_ejecucion: str, nombre_archivo: str):
    """Redirige a una URL firmada de Supabase Storage (generados/<modulo>/<id>/<archivo>)."""
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