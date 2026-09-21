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

import shutil
import tempfile
import threading
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
    supabase_storage.subir_bytes(ruta_storage, ruta_local.read_bytes())
    return ruta_storage


@router.post("")
async def iniciar_procesamiento(excel: UploadFile = File(...)):
    """
    El Excel (Consolidado General) se sube como adjunto en cada
    ejecución. Los PDFs ya no se suben: se toman directamente de la
    sección "Base de Datos", filtrando por modulo = "no_programados".
    """
    if not excel.filename or not excel.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=400,
            detail="El Consolidado General debe ser un archivo .xlsx o .xlsm.",
        )

    archivos = listar_archivos_por_modulo(NOMBRE_MODULO)
    archivos_pdf = [
        a for a in archivos
        if a["nombre_original"].lower().endswith(".pdf")
    ]

    if not archivos_pdf:
        raise HTTPException(
            status_code=400,
            detail=(
                "No hay ningún PDF en la Base de Datos con el módulo "
                "'No Programados'. Súbelos primero desde esa sección."
            ),
        )

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"no_programados_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_bd = carpeta_entrada / "BD"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_bd.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
        ruta_excel = carpeta_entrada / excel.filename
        ruta_excel.write_bytes(await excel.read())

        pdfs_guardados = 0
        pdfs_fallidos = []
        for pdf in archivos_pdf:
            try:
                destino = _ruta_local_para_archivo(carpeta_bd, pdf)
            except ValueError:
                pdfs_fallidos.append(pdf["nombre_original"])
                continue
            destino.parent.mkdir(parents=True, exist_ok=True)
            try:
                destino.write_bytes(supabase_storage.descargar_bytes(pdf["ruta"]))
            except Exception:
                # Si un PDF puntual falla al descargar de Storage, se
                # omite y se sigue con los demás en vez de tumbar todo
                # el proceso.
                pdfs_fallidos.append(pdf["nombre_original"])
                continue
            pdfs_guardados += 1

        if pdfs_guardados == 0:
            raise HTTPException(
                status_code=400,
                detail="No se pudo descargar ningún PDF desde la Base de Datos.",
            )
    except HTTPException:
        # El hilo nunca arranca, así que la limpieza es responsabilidad
        # de este endpoint.
        shutil.rmtree(carpeta_temporal, ignore_errors=True)
        raise
    except Exception:
        # Cualquier otro error (ej. lectura del excel, IO en disco) tampoco
        # debe dejar la carpeta temporal huérfana.
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
                resultado["pdfs_fallidos_al_descargar"] = pdfs_fallidos

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