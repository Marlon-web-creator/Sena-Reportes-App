"""
routers/correo_router.py

Endpoints del módulo "Correo de Aprendices": cruza documento (columna D
del consolidado) contra documento/correo (columnas B/F de varios xls).

Los archivos de entrada y los intermedios se manejan en una carpeta
temporal (se borra al terminar la petición). El archivo de RESULTADO se
sube a Supabase Storage para que persista entre despliegues de Render.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

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
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"correos_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_entrada.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
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
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error procesando los archivos: {e}")

        # Subir el resultado a Supabase Storage y reemplazar la ruta
        # local por la ruta dentro del bucket ANTES de guardar en BD.
        ruta_storage = _subir_generado(resultado["archivo_generado"], id_ejecucion)
        resultado["archivo_generado"] = ruta_storage

        parametros = {
            "consolidado": consolidado.filename,
            "n_archivos_xls": len(rutas_xls),
            "fila_inicio_consolidado": fila_inicio_consolidado,
            "col_documento_consolidado": col_documento_consolidado,
            "fila_inicio_xls": fila_inicio_xls,
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