"""
routers/consolidador_router.py

Endpoints del módulo "Consolidador NoAprobados / PorEvaluar", equivalente
web del script NoAprobados.py.

Los archivos de entrada y los intermedios que genera `no_aprobados` se
manejan en una carpeta temporal (se borra al terminar la petición, ya
que Render no garantiza disco persistente). Los archivos de RESULTADO
se suben a Supabase Storage para que persistan entre despliegues.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse

import supabase_storage
from database import guardar_ejecucion, listar_ejecuciones
from modules import no_aprobados

router = APIRouter(prefix="/api/consolidador", tags=["Consolidador NoAprobados"])

EXTENSIONES_PERMITIDAS = {".xls", ".xlsx"}

NOMBRE_MODULO = "consolidador_no_aprobados"


def _subir_generados(archivos_generados: dict, id_ejecucion: str) -> dict:
    """
    Sube a Supabase Storage cada archivo que generó no_aprobados.consolidar()
    (hoy en la carpeta temporal local) y devuelve un nuevo diccionario
    clave -> path dentro del bucket. Eso es lo que se guarda en la BD y
    lo que se usa para las descargas.
    """
    generados_storage = {}

    for clave, ruta_local in archivos_generados.items():
        ruta_local = Path(ruta_local)

        if not ruta_local.exists():
            continue

        ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{ruta_local.name}"
        supabase_storage.subir_bytes(ruta_storage, ruta_local.read_bytes())
        generados_storage[clave] = ruta_storage

    return generados_storage


@router.post("")
async def ejecutar_consolidador(
    filtro: str = Form(...),               # "NO APROBADO" o "POR EVALUAR"
    generar_general: bool = Form(False),
    archivos: list[UploadFile] = File(...),
):
    if filtro not in no_aprobados.FILTROS:
        raise HTTPException(status_code=400, detail=f"Filtro no válido: {filtro}")
    if not archivos:
        raise HTTPException(status_code=400, detail="Debes subir al menos un archivo.")

    # Carpeta temporal única por ejecución. Se borra al final del request
    # (éxito o error), porque en Render es disco efímero de todos modos.
    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"consolidador_{id_ejecucion}_"))
    carpeta_entrada = carpeta_temporal / "entrada"
    carpeta_salida = carpeta_temporal / "salida"
    carpeta_entrada.mkdir(parents=True, exist_ok=True)
    carpeta_salida.mkdir(parents=True, exist_ok=True)

    try:
        rutas_guardadas = []
        for archivo in archivos:
            extension = Path(archivo.filename).suffix.lower()
            if extension not in EXTENSIONES_PERMITIDAS:
                continue  # se ignoran archivos que no sean .xls/.xlsx
            destino = carpeta_entrada / archivo.filename
            with destino.open("wb") as f:
                shutil.copyfileobj(archivo.file, f)
            rutas_guardadas.append(destino)

        if not rutas_guardadas:
            raise HTTPException(status_code=400, detail="Ninguno de los archivos subidos es .xls o .xlsx.")

        try:
            resultado = no_aprobados.consolidar(
                archivos=rutas_guardadas,
                valor_filtro=filtro,
                generar_general=generar_general,
                salida_dir=carpeta_salida,
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error procesando los archivos: {e}")

        # Subir los resultados a Supabase Storage ANTES de borrar la
        # carpeta temporal y de guardar el registro en la BD.
        archivos_generados_storage = _subir_generados(
            resultado.get("archivos_generados") or {},
            id_ejecucion,
        )
        resultado["archivos_generados"] = archivos_generados_storage

        parametros = {"filtro": filtro, "generar_general": generar_general, "n_archivos": len(rutas_guardadas)}
        id_bd = guardar_ejecucion(
            modulo=NOMBRE_MODULO,
            parametros=parametros,
            resultado=resultado["stats"],
            archivos_generados=archivos_generados_storage,
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
    Asume la convención de rutas usada en _subir_generados():
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