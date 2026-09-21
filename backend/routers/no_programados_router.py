"""
routers/no_programados_router.py

Endpoints del módulo "Verificador No Programados". El procesamiento corre
en un hilo aparte: el POST devuelve de inmediato un id_ejecucion, y el
frontend consulta /progreso/{id} cada cierto tiempo.

Los PDFs NO se suben por formulario: se toman de la sección "Base de
Datos" filtrando por modulo = "no_programados". El Consolidado General
SÍ se sube como adjunto en cada ejecución (campo "excel").

La descarga de PDFs desde Supabase Storage se hace en paralelo con
timeout por archivo, para que un PDF colgado no bloquee todo.
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
    obtener_ruta_carpeta,
)
from modules import no_programados

# ============================================================
# LOGGING
# ============================================================
logger = logging.getLogger("no_programados.router")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] np.router: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(_h)
    logger.propagate = False

router = APIRouter(prefix="/api/no-programados", tags=["No Programados"])

NOMBRE_MODULO = "no_programados"

# >>> Descarga paralela y timeout por archivo
MAX_DESCARGAS_PARALELAS = int(os.environ.get("NP_DESCARGAS_PARALELAS") or 8)
TIMEOUT_DESCARGA_POR_ARCHIVO = float(os.environ.get("NP_TIMEOUT_DESCARGA") or 45)


def _ruta_segura(base: Path, nombre_relativo: str) -> Path:
    """Evita path traversal: normaliza y rechaza rutas que se salgan de `base`."""
    limpio = nombre_relativo.replace("\\", "/").lstrip("/")
    destino = (base / limpio).resolve()
    base_resuelta = base.resolve()
    if base_resuelta != destino and base_resuelta not in destino.parents:
        raise ValueError(f"Ruta de archivo no válida: {nombre_relativo}")
    return destino


def _ruta_local_para_archivo(carpeta_bd: Path, archivo: dict) -> Path:
    """
    Reconstruye, dentro de la carpeta temporal, la misma ruta de
    subcarpetas que el archivo tiene en la sección "Base de Datos"
    (carpeta_id -> nombre).
    """
    ruta_carpetas = obtener_ruta_carpeta(archivo["carpeta_id"])
    subcarpetas = [c["nombre"] for c in ruta_carpetas]
    return _ruta_segura(carpeta_bd, "/".join(subcarpetas + [archivo["nombre_original"]]))


def _subir_generado(ruta_local, id_ejecucion: str) -> str:
    """
    Sube el archivo resultado (hoy en la carpeta temporal local) a
    Supabase Storage y devuelve el path dentro del bucket.
    """
    ruta_local = Path(ruta_local)
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{ruta_local.name}"
    logger.info("Subiendo generado a Storage: %s", ruta_storage)
    t = time.time()
    supabase_storage.subir_bytes(ruta_storage, ruta_local.read_bytes())
    logger.info("Subida completada en %.1fs", time.time() - t)
    return ruta_storage


@router.post("")
async def iniciar_procesamiento(excel: UploadFile = File(...)):
    """
    El Excel (Consolidado General) se sube como adjunto en cada
    ejecución. Los PDFs se toman de la sección "Base de Datos" con
    modulo = "no_programados".
    """
    t_endpoint = time.time()
    logger.info("POST /api/no-programados — excel=%s", excel.filename)

    if not excel.filename or not excel.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400,
            detail="El Consolidado General debe ser un archivo .xlsx o .xlsm.",
        )

    logger.info("Listando archivos de BD con módulo=%s…", NOMBRE_MODULO)
    archivos = listar_archivos_por_modulo(NOMBRE_MODULO)
    archivos_pdf = [
        a for a in archivos
        if a["nombre_original"].lower().endswith(".pdf")
    ]
    logger.info(
        "Archivos en BD con módulo '%s': %d total, %d son PDF",
        NOMBRE_MODULO, len(archivos), len(archivos_pdf),
    )

    if not archivos_pdf:
        raise HTTPException(
            status_code=400,
            detail=(
                "No hay ningún PDF en la Base de Datos con el módulo "
                "'No Programados'. Súbelos primero desde esa sección."
            ),
        )

    id_ejecucion = uuid.uuid4().hex[:10]
    logger.info("ID de ejecución asignado: %s", id_ejecucion)

    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"no_programados_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_bd = carpeta_entrada / "BD"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_bd.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)
    logger.info("Carpeta temporal creada: %s", carpeta_temporal)

    try:
        ruta_excel = carpeta_entrada / excel.filename
        t = time.time()
        ruta_excel.write_bytes(await excel.read())
        logger.info("Excel guardado en disco: %s (%.1fs)", ruta_excel, time.time() - t)

        # ------------------------------------------------------------
        # Descarga de PDFs desde Supabase Storage (en paralelo).
        # Timeout por archivo: un PDF colgado no bloquea toda la
        # ejecución, se marca como fallido y se sigue.
        # ------------------------------------------------------------
        logger.info(
            "Ejecución %s: descargando %d PDF(s) desde Storage (paralelo=%d, timeout=%.0fs)…",
            id_ejecucion, len(archivos_pdf),
            MAX_DESCARGAS_PARALELAS, TIMEOUT_DESCARGA_POR_ARCHIVO,
        )
        t_descarga = time.time()
        pdfs_guardados = 0
        pdfs_fallidos = []

        def _descargar_uno(pdf: dict):
            """Devuelve (nombre, ok: bool, error: str|None)."""
            nombre = pdf.get("nombre_original", "?")
            try:
                destino = _ruta_local_para_archivo(carpeta_bd, pdf)
            except ValueError as e:
                return nombre, False, f"ruta inválida: {e}"
            destino.parent.mkdir(parents=True, exist_ok=True)
            try:
                datos = supabase_storage.descargar_bytes(pdf["ruta"])
                destino.write_bytes(datos)
                return nombre, True, None
            except Exception as e:
                return nombre, False, f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(
            max_workers=MAX_DESCARGAS_PARALELAS, thread_name_prefix="np-dl"
        ) as pool:
            futuros = {
                pool.submit(_descargar_uno, pdf): pdf["nombre_original"]
                for pdf in archivos_pdf
            }
            total = len(futuros)
            procesados = 0

            for fut in as_completed(futuros):
                nombre = futuros[fut]
                try:
                    nombre, ok, err = fut.result(timeout=TIMEOUT_DESCARGA_POR_ARCHIVO)
                except FuturesTimeout:
                    logger.warning(
                        "Timeout descargando %s (> %.0fs), se omite",
                        nombre, TIMEOUT_DESCARGA_POR_ARCHIVO,
                    )
                    pdfs_fallidos.append(nombre)
                    procesados += 1
                    continue
                except Exception:
                    logger.exception("Error inesperado descargando %s", nombre)
                    pdfs_fallidos.append(nombre)
                    procesados += 1
                    continue

                if ok:
                    pdfs_guardados += 1
                else:
                    logger.warning("Fallo descargando %s: %s", nombre, err)
                    pdfs_fallidos.append(nombre)

                procesados += 1
                if procesados % 20 == 0 or procesados == total:
                    seg = time.time() - t_descarga
                    v = procesados / seg if seg > 0 else 0
                    logger.info(
                        "Descarga Storage: %d/%d PDFs (%.1fs, %.1f PDF/s, %d ok, %d fallidos)",
                        procesados, total, seg, v, pdfs_guardados, len(pdfs_fallidos),
                    )

        if pdfs_guardados == 0:
            raise HTTPException(
                status_code=400,
                detail="No se pudo descargar ningún PDF desde la Base de Datos.",
            )

        logger.info(
            "Descarga completa: %d PDFs guardados, %d fallidos, total %.1fs",
            pdfs_guardados, len(pdfs_fallidos), time.time() - t_descarga,
        )
    except HTTPException:
        logger.warning(
            "Cancelando ejecución %s (HTTPException). Limpiando temporal.",
            id_ejecucion,
        )
        shutil.rmtree(carpeta_temporal, ignore_errors=True)
        raise
    except Exception:
        logger.exception(
            "Error preparando ejecución %s. Limpiando temporal.", id_ejecucion,
        )
        shutil.rmtree(carpeta_temporal, ignore_errors=True)
        raise

    progreso.iniciar(id_ejecucion)
    logger.info("Estado de progreso inicializado para %s", id_ejecucion)

    def _tarea():
        logger.info("Hilo de procesamiento %s: arrancando…", id_ejecucion)
        t_hilo = time.time()
        try:
            def callback(mensaje, actual, total):
                progreso.actualizar(id_ejecucion, mensaje, actual, total)

            try:
                logger.info("Hilo %s: llamando a no_programados.procesar()", id_ejecucion)
                resultado = no_programados.procesar(
                    archivo_excel=ruta_excel,
                    carpeta_bd=carpeta_bd,
                    salida_dir=carpeta_salida,
                    progress_callback=callback,
                )
                logger.info(
                    "Hilo %s: procesar() terminó en %.1fs",
                    id_ejecucion, time.time() - t_hilo,
                )

                ruta_storage = _subir_generado(resultado["archivo_generado"], id_ejecucion)
                resultado["archivo_generado"] = ruta_storage
                resultado["pdfs_fallidos_al_descargar"] = pdfs_fallidos

                logger.info("Hilo %s: guardando ejecución en BD…", id_ejecucion)
                guardar_ejecucion(
                    modulo=NOMBRE_MODULO,
                    parametros={
                        "excel": excel.filename,
                        "n_pdfs": pdfs_guardados,
                    },
                    resultado=resultado,
                    archivos_generados={"PROCESADO": ruta_storage},
                )
                progreso.finalizar_ok(id_ejecucion, resultado)
                logger.info(
                    "Hilo %s: FIN OK en %.1fs total",
                    id_ejecucion, time.time() - t_hilo,
                )
            except Exception as e:
                logger.exception("Hilo %s: error en procesar(): %s", id_ejecucion, e)
                progreso.finalizar_error(id_ejecucion, str(e))
        finally:
            logger.info("Hilo %s: limpiando carpeta temporal", id_ejecucion)
            shutil.rmtree(carpeta_temporal, ignore_errors=True)

    threading.Thread(target=_tarea, daemon=True, name=f"np-{id_ejecucion}").start()
    logger.info(
        "POST /api/no-programados respondido en %.1fs con id=%s",
        time.time() - t_endpoint, id_ejecucion,
    )

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
    logger.info("Descarga solicitada: %s", ruta_storage)

    if not supabase_storage.existe_archivo(ruta_storage):
        logger.warning("Archivo no encontrado en Storage: %s", ruta_storage)
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    url = supabase_storage.crear_url_firmada(ruta_storage, nombre_descarga=nombre_archivo)

    if not url:
        logger.error("No se pudo firmar URL para %s", ruta_storage)
        raise HTTPException(status_code=500, detail="No se pudo generar el enlace de descarga.")

    return RedirectResponse(url)


@router.get("/historial")
async def historial(limite: int = 20):
    logger.info("GET /historial limite=%d", limite)
    return listar_ejecuciones(modulo=NOMBRE_MODULO, limite=limite)