"""
routers/horas_instructores_router.py

Endpoints del módulo "Comparador de Horas Instructores". Igual que
No Programados: el POST devuelve de inmediato un id_ejecucion, el
procesamiento corre en un hilo aparte y el frontend consulta
/progreso/{id} cada cierto tiempo.

Los Excels de horas programadas (ej. HORAS_INSTRUCTORES_CONTRATISTA.xlsx)
NO se suben por formulario: se toman de la sección "Base de Datos"
filtrando por módulo "Horas Instructores". Cualquier variación de
mayúsculas, espacios o guiones ("horas instructores", "HORAS-INSTRUCTORES",
"horas_instructores") cuenta como el mismo módulo.

Lo que SÍ se sube en cada ejecución es el "Reporte Ejecución Horas
Instructor" (campo "reporte") y el mes a comparar (campo "mes", 1-12), porque
el reporte no indica a qué mes corresponde.
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

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

import progreso
import supabase_storage
from database import (
    guardar_ejecucion,
    listar_archivos_por_modulo,
    listar_ejecuciones,
)
from modules import horas_instructores
from modules.file_utils import es_excel

logger = logging.getLogger("horas_instructores.router")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] hi.router: %(message)s", datefmt="%H:%M:%S",
    ))
    logger.addHandler(_h)
    logger.propagate = False

router = APIRouter(prefix="/api/horas-instructores", tags=["Horas Instructores"])

NOMBRE_MODULO = "horas_instructores"

# Nombres de módulo que se aceptan en la BD (ya normalizados: minúsculas y
# con "_"; listar_archivos_por_modulo normaliza espacios y guiones).
MODULOS_ACEPTADOS = ("horas_instructores", "horas_instructor")

MAX_DESCARGAS_PARALELAS = int(os.environ.get("HI_DESCARGAS_PARALELAS") or 8)
TIMEOUT_DESCARGA_POR_ARCHIVO = float(os.environ.get("HI_TIMEOUT_DESCARGA") or 45)


def _subir_generado(ruta_local, id_ejecucion: str) -> str:
    """Sube el Excel resultado a Supabase Storage y devuelve su path en el bucket."""
    ruta_local = Path(ruta_local)
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{ruta_local.name}"
    supabase_storage.subir_bytes(ruta_storage, ruta_local.read_bytes())
    return ruta_storage


def _archivos_bd_excel() -> list[dict]:
    """Excels de la BD con módulo 'Horas Instructores' (sin duplicados por id)."""
    vistos: dict[int, dict] = {}
    for modulo in MODULOS_ACEPTADOS:
        for a in listar_archivos_por_modulo(modulo):
            vistos.setdefault(a["id"], a)
    return [
        a for a in vistos.values()
        if a["nombre_original"].lower().endswith(horas_instructores.EXTENSIONES_BD)
        and not a["nombre_original"].startswith("~$")
    ]


@router.post("")
async def iniciar_procesamiento(
    reporte: UploadFile = File(...),
    mes: int = Form(...),
):
    logger.info("POST /api/horas-instructores — reporte=%s mes=%s", reporte.filename, mes)

    if not reporte.filename or not es_excel(reporte.filename):
        raise HTTPException(
            status_code=400,
            detail="El reporte debe ser un archivo Excel (.xls, .xlsx o .xlsm).",
        )
    if mes not in horas_instructores.MESES:
        raise HTTPException(status_code=400, detail="El mes debe estar entre 1 y 12.")

    archivos_excel = _archivos_bd_excel()
    if not archivos_excel:
        raise HTTPException(
            status_code=400,
            detail=(
                "No hay ningún Excel (.xlsx/.xlsm) en la Base de Datos con el "
                "módulo 'Horas Instructores'. Súbelo primero desde esa sección."
            ),
        )

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"horas_instructores_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_bd = carpeta_entrada / "BD"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_bd.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
        ruta_reporte = carpeta_entrada / Path(reporte.filename).name
        ruta_reporte.write_bytes(await reporte.read())

        # Cada archivo va en su propia subcarpeta (id) para que dos archivos
        # con el mismo nombre en carpetas distintas de la BD no se pisen.
        def _descargar_uno(archivo: dict):
            nombre = archivo["nombre_original"]
            try:
                destino = carpeta_bd / str(archivo["id"]) / Path(nombre).name
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(supabase_storage.descargar_bytes(archivo["ruta"]))
                return nombre, True, None
            except Exception as e:
                return nombre, False, f"{type(e).__name__}: {e}"

        t0 = time.time()
        guardados = 0
        fallidos: list[str] = []

        pool = ThreadPoolExecutor(max_workers=MAX_DESCARGAS_PARALELAS, thread_name_prefix="hi-dl")
        futuros = {pool.submit(_descargar_uno, a): a["nombre_original"] for a in archivos_excel}
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
                detail="No se pudo descargar ningún Excel desde la Base de Datos.",
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
    nombre_reporte = reporte.filename

    def _tarea():
        try:
            def callback(mensaje, actual, total):
                progreso.actualizar(id_ejecucion, mensaje, actual, total)

            try:
                resultado = horas_instructores.procesar(
                    archivo_reporte=ruta_reporte,
                    carpeta_bd=carpeta_bd,
                    salida_dir=carpeta_salida,
                    mes=mes,
                    progress_callback=callback,
                )
                ruta_storage = _subir_generado(resultado["archivo_generado"], id_ejecucion)
                resultado["archivo_generado"] = ruta_storage
                resultado["excels_fallidos_al_descargar"] = fallidos

                guardar_ejecucion(
                    modulo=NOMBRE_MODULO,
                    parametros={
                        "reporte": nombre_reporte,
                        "mes": mes,
                        "mes_nombre": resultado["mes_nombre"],
                        "n_excels": guardados,
                    },
                    resultado=resultado,
                    archivos_generados={"COMPARATIVO": ruta_storage},
                )
                progreso.finalizar_ok(id_ejecucion, resultado)
            except Exception as e:
                logger.exception("Hilo %s: error: %s", id_ejecucion, e)
                progreso.finalizar_error(id_ejecucion, str(e))
        finally:
            shutil.rmtree(carpeta_temporal, ignore_errors=True)

    threading.Thread(target=_tarea, daemon=True, name=f"hi-{id_ejecucion}").start()
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
