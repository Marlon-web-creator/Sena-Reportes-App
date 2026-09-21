"""
routers/no_programados_router.py

Endpoints del módulo "Verificador No Programados". El procesamiento corre
en un hilo aparte: el POST devuelve de inmediato un id_ejecucion, y el
frontend consulta /progreso/{id} cada cierto tiempo.

CAMBIO: este módulo YA NO recibe los PDFs como adjuntos del formulario.
En su lugar, toma todos los PDFs que haya en la sección "Base de Datos"
etiquetados con el módulo "no_programados" (el campo `modulo` que ya se
asigna ahí al subir archivos): debe haber al menos un PDF con ese
módulo. El Consolidado General SÍ se sigue subiendo como adjunto en
cada ejecución (form-data, campo "excel"), porque cambia con cada
verificación.

Entrada/intermedios (excel + PDFs) en carpeta temporal: el excel llega
por upload y los PDFs se descargan desde Supabase Storage. Como el
procesamiento sigue corriendo en segundo plano después de responder el
POST, la carpeta temporal se borra DENTRO del hilo (en su finally), no
en el endpoint. El archivo de RESULTADO se sube a Supabase Storage
antes de marcar la ejecución como terminada.
"""

import logging
import shutil
import tempfile
import threading
import time
import uuid
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

    Esto es importante porque no_programados.construir_mapa_ficha_pdf()
    empareja cada PDF con su ficha mirando los NOMBRES DE CARPETA en la
    ruta (ej. BD/2904878/archivo.pdf). Si los PDFs están organizados en
    subcarpetas por ficha dentro de "Base de Datos", esa organización se
    respeta igual que antes, cuando se subía la carpeta completa.
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
    ejecución. Los PDFs ya no se suben: se toman directamente de la
    sección "Base de Datos", filtrando por modulo = "no_programados".
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
        # Descarga de PDFs desde Supabase Storage.
        # Nota: con 600+ PDFs esto puede tardar. Loggeamos cada 20.
        # ------------------------------------------------------------
        logger.info(
            "Ejecución %s: descargando %d PDF(s) desde Storage…",
            id_ejecucion, len(archivos_pdf),
        )
        t_descarga = time.time()
        pdfs_guardados = 0
        pdfs_fallidos = []
        for i, pdf in enumerate(archivos_pdf, start=1):
            try:
                destino = _ruta_local_para_archivo(carpeta_bd, pdf)
            except ValueError:
                logger.warning("Ruta inválida, se omite: %s", pdf.get("nombre_original"))
                pdfs_fallidos.append(pdf["nombre_original"])
                continue
            destino.parent.mkdir(parents=True, exist_ok=True)
            try:
                destino.write_bytes(supabase_storage.descargar_bytes(pdf["ruta"]))
            except Exception:
                # Si un PDF puntual falla al descargar de Storage, se
                # omite y se sigue con los demás en vez de tumbar todo
                # el proceso.
                logger.exception(
                    "Fallo descargando PDF %s (%s)",
                    pdf.get("nombre_original"), pdf.get("ruta"),
                )
                pdfs_fallidos.append(pdf["nombre_original"])
                continue
            pdfs_guardados += 1
            if i % 20 == 0 or i == len(archivos_pdf):
                seg = time.time() - t_descarga
                v = i / seg if seg > 0 else 0
                logger.info(
                    "Descarga Storage: %d/%d PDFs (%.1fs, %.1f PDF/s, %d fallidos)",
                    i, len(archivos_pdf), seg, v, len(pdfs_fallidos),
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
        # El hilo nunca arranca, así que la limpieza es responsabilidad
        # de este endpoint.
        logger.warning("Cancelando ejecución %s (HTTPException). Limpiando temporal.", id_ejecucion)
        shutil.rmtree(carpeta_temporal, ignore_errors=True)
        raise
    except Exception:
        # Cualquier otro error (ej. lectura del excel, IO en disco) tampoco
        # debe dejar la carpeta temporal huérfana.
        logger.exception("Error preparando ejecución %s. Limpiando temporal.", id_ejecucion)
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

                # Subir el resultado a Supabase Storage ANTES de guardar
                # en BD y de avisar al frontend que ya terminó.
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
            # Aquí sí se puede borrar: el hilo ya terminó de usar la
            # carpeta temporal (subió lo que necesitaba a Storage).
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