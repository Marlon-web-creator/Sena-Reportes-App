"""
routers/juicios_practica_router.py

Endpoints del módulo "Juicios posteriores a Etapa Práctica": revisa en uno o
varios Reportes de Juicios de Evaluación (uno por ficha) si hay juicios
lectivos APROBADOS con fecha posterior a la aprobación del juicio de
"2 - RESULTADOS DE APRENDIZAJE ETAPA PRACTICA".

Las columnas y la fila de inicio se detectan automáticamente, así que el
usuario solo sube los reportes.

Los archivos de entrada se manejan en una carpeta temporal (se borra al
terminar la petición). El archivo de RESULTADO se sube a Supabase Storage
para que persista entre despliegues de Render.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

import supabase_storage
from database import guardar_ejecucion, listar_ejecuciones
from modules import juicios_practica
from modules.file_utils import es_excel

router = APIRouter(prefix="/api/juicios-practica", tags=["Juicios posteriores a Práctica"])

NOMBRE_MODULO = "juicios_practica"


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
async def ejecutar_juicios_practica(
    reportes: list[UploadFile] = File(...),
    solo_fecha: bool = False,
):
    """
    reportes: uno o varios Reportes de Juicios de Evaluación (.xls/.xlsx/.xlsm).
    solo_fecha: si es true compara solo el día; por defecto compara fecha y hora.
    """
    if not reportes:
        raise HTTPException(status_code=400, detail="Debes subir al menos un reporte de juicios.")

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"juicios_practica_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_entrada.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
        rutas = []
        for archivo in reportes:
            if not es_excel(archivo.filename):
                continue
            destino = carpeta_entrada / archivo.filename
            with destino.open("wb") as f:
                shutil.copyfileobj(archivo.file, f)
            rutas.append(destino)

        if not rutas:
            raise HTTPException(status_code=400, detail="Ninguno de los archivos subidos es .xls, .xlsx o .xlsm.")

        try:
            resultado = juicios_practica.verificar_juicios_posteriores_practica(
                archivos=rutas,
                salida_dir=carpeta_salida,
                solo_fecha=solo_fecha,
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
            "n_reportes": len(rutas),
            "solo_fecha": solo_fecha,
        }
        id_bd = guardar_ejecucion(
            modulo=NOMBRE_MODULO,
            parametros=parametros,
            resultado=resultado,
            archivos_generados={"JUICIOS_POSTERIORES_PRACTICA": ruta_storage},
        )

        return {"id_ejecucion": id_ejecucion, "id_bd": id_bd, **resultado}
    finally:
        shutil.rmtree(carpeta_temporal, ignore_errors=True)


@router.get("/descargar/{id_ejecucion}/{nombre_archivo}")
async def descargar_resultado(id_ejecucion: str, nombre_archivo: str):
    """
    Redirige a una URL firmada temporal de Supabase Storage.
    Igual que en archivos_router._redirigir_a_url_firmada: NO se
    pre-valida con existe_archivo() para evitar falsos negativos;
    si crear_url_firmada() no devuelve URL, entonces sí es 404.
    """
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{nombre_archivo}"

    url = supabase_storage.crear_url_firmada(
        ruta_storage, nombre_descarga=nombre_archivo
    )

    if not url:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    return RedirectResponse(url)


@router.get("/historial")
async def historial(limite: int = 20):
    return listar_ejecuciones(modulo=NOMBRE_MODULO, limite=limite)