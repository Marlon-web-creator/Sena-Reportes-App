"""
routers/depuracion_router.py

Endpoints del módulo "Depuración de Por Evaluar", equivalente web de
Depuracion.py.

Entrada/intermedios en carpeta temporal (se borra al terminar la
petición). El archivo de RESULTADO se sube a Supabase Storage para que
persista entre despliegues de Render.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

import supabase_storage
from database import guardar_ejecucion, listar_ejecuciones
from modules import depuracion

router = APIRouter(prefix="/api/depuracion", tags=["Depuración"])

EXTENSIONES_PERMITIDAS = {".xlsx"}

NOMBRE_MODULO = "depuracion"


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
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"depuracion_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_entrada.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
        ruta_entrada = carpeta_entrada / archivo.filename
        with ruta_entrada.open("wb") as f:
            shutil.copyfileobj(archivo.file, f)

        try:
            resultado = depuracion.depurar(
                archivo=ruta_entrada,
                limite=limite,
                salida_dir=carpeta_salida,
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error procesando el archivo: {e}")

        # Subir el resultado a Supabase Storage y reemplazar la ruta
        # local por la ruta dentro del bucket ANTES de guardar en BD.
        ruta_storage = _subir_generado(resultado["archivo_generado"], id_ejecucion)
        resultado["archivo_generado"] = ruta_storage

        parametros = {"limite": limite, "archivo": archivo.filename}
        id_bd = guardar_ejecucion(
            modulo=NOMBRE_MODULO,
            parametros=parametros,
            resultado=resultado,
            archivos_generados={"PROCESADO": ruta_storage},
        )

        return {
            "id_ejecucion": id_ejecucion,
            "id_bd": id_bd,
            **resultado,
        }
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