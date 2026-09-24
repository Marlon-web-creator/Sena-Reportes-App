"""
routers/correo_router.py

Endpoints del módulo "Correo de Aprendices": recibe el consolidado y le
agrega una columna con el correo de cada aprendiz, generado de forma
procedural a partir de nombres, apellidos y documento (ya no se suben
archivos xls con correos).

Los archivos de entrada y los intermedios se manejan en una carpeta
temporal (se borra al terminar la petición). El archivo de RESULTADO se
sube a Supabase Storage para que persista entre despliegues de Render.
"""

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

import supabase_storage
from database import guardar_ejecucion, listar_ejecuciones
from modules import correo_aprendices
from modules.file_utils import es_excel

router = APIRouter(prefix="/api/correos", tags=["Correo de Aprendices"])

NOMBRE_MODULO = "correo_aprendices"


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
async def ejecutar_correos(
    consolidado: UploadFile = File(...),
    plantilla: str = Form(correo_aprendices.PLANTILLA_DEFECTO),
    dominio: str = Form(correo_aprendices.DOMINIO_DEFECTO),
    # Opcionales: si no se envían (o vienen en 0) se detectan por los encabezados.
    fila_encabezado: Optional[int] = Form(None),
    col_documento: Optional[int] = Form(None),
):
    if not es_excel(consolidado.filename):
        raise HTTPException(status_code=400, detail="El consolidado debe ser .xls, .xlsx o .xlsm.")

    try:
        correo_aprendices.validar_plantilla(plantilla)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    fila_encabezado = fila_encabezado or None
    col_documento = col_documento or None

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"correos_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_entrada.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
        ruta_consolidado = carpeta_entrada / consolidado.filename
        with ruta_consolidado.open("wb") as f:
            shutil.copyfileobj(consolidado.file, f)

        try:
            resultado = correo_aprendices.generar_correos_consolidado(
                consolidado=ruta_consolidado,
                salida_dir=carpeta_salida,
                plantilla=plantilla,
                dominio=dominio,
                fila_encabezado=fila_encabezado,
                col_documento=col_documento,
            )
        except HTTPException:
            raise
        except ValueError as e:
            # Estructura del consolidado no reconocida / plantilla inválida
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error procesando el consolidado: {e}")

        # Subir el resultado a Supabase Storage y reemplazar la ruta
        # local por la ruta dentro del bucket ANTES de guardar en BD.
        ruta_storage = _subir_generado(resultado["archivo_generado"], id_ejecucion)
        resultado["archivo_generado"] = ruta_storage

        parametros = {
            "consolidado": consolidado.filename,
            "plantilla": plantilla,
            "dominio": dominio,
            "fila_encabezado": fila_encabezado,
            "col_documento": col_documento,
        }
        id_bd = guardar_ejecucion(
            modulo=NOMBRE_MODULO,
            parametros=parametros,
            resultado=resultado,
            archivos_generados={"CON_CORREOS": ruta_storage},
        )

        return {"id_ejecucion": id_ejecucion, "id_bd": id_bd, **resultado}
    finally:
        shutil.rmtree(carpeta_temporal, ignore_errors=True)


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