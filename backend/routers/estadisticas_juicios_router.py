"""
routers/estadisticas_juicios_router.py

Endpoints del módulo "Estadísticas y proyección de juicios".

Mismo patrón que depuracion_router.py: el archivo subido vive en una
carpeta temporal que se borra al terminar la petición. El Excel de
resultados se sube a Supabase Storage (persiste entre despliegues de
Render) y se descarga con una URL firmada. El JSON resultante se guarda
en BD (historial) y alimenta la proyección histórica de las ejecuciones
siguientes.
"""

import json
import shutil
import tempfile
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

import supabase_storage
from database import guardar_ejecucion, listar_ejecuciones
from modules import estadisticas_juicios, estadisticas_juicios_excel

router = APIRouter(prefix="/api/estadisticas-juicios", tags=["Estadísticas de juicios"])

EXTENSIONES_PERMITIDAS = {".xlsx"}
NOMBRE_MODULO = "estadisticas_juicios"


def _subir_excel(ruta_local, id_ejecucion: str) -> str:
    """Sube el Excel a Supabase Storage y devuelve la ruta dentro del bucket."""
    ruta_local = Path(ruta_local)
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{ruta_local.name}"
    supabase_storage.subir_bytes(ruta_storage, ruta_local.read_bytes())
    return ruta_storage


def _cargar_historico(limite: int = 60) -> list[dict]:
    """
    Reconstruye [{fecha, total_pendientes}] desde ejecuciones anteriores.
    Usa 'fecha_reporte' y 'total_pendientes' guardados dentro del resultado,
    así no depende del nombre de las columnas de timestamp de la BD.
    Es tolerante: cualquier registro con formato inesperado se ignora.
    """
    try:
        registros = listar_ejecuciones(modulo=NOMBRE_MODULO, limite=limite) or []
    except Exception:
        return []

    historico = []
    for reg in registros:
        res = reg.get("resultado") if isinstance(reg, dict) else None
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except ValueError:
                continue
        try:
            resumen = res["estadisticas"]["resumen"]
            historico.append({
                "fecha": date.fromisoformat(resumen["fecha_reporte"]),
                "total_pendientes": int(resumen["total_pendientes"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return historico


@router.post("")
async def ejecutar_estadisticas(
    archivo: UploadFile = File(...),
    top_n: int = Form(10),
    # RAPs que ve cada aprendiz por trimestre (~10 según la operación)
    raps_por_trimestre: int = Form(10),
    # Opcional: JSON con supuestos, p. ej. {"base": {"con_sync": 0.8}}
    supuestos: Optional[str] = Form(None),
    fila_encabezado: Optional[int] = Form(None),
):
    if Path(archivo.filename).suffix.lower() not in EXTENSIONES_PERMITIDAS:
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx.")
    if not (1 <= top_n <= 50):
        raise HTTPException(status_code=400, detail="El top debe estar entre 1 y 50.")
    if not (0 <= raps_por_trimestre <= 100):
        raise HTTPException(status_code=400, detail="Los RAPs por trimestre deben estar entre 0 y 100.")

    supuestos_dict = None
    if supuestos:
        try:
            supuestos_dict = json.loads(supuestos)
            if not isinstance(supuestos_dict, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="'supuestos' debe ser un JSON de objetos.")

    id_ejecucion = uuid.uuid4().hex[:10]
    carpeta_temporal = Path(tempfile.mkdtemp(prefix=f"estjuicios_{id_ejecucion}_"))

    try:
        ruta_entrada = carpeta_temporal / Path(archivo.filename).name
        with ruta_entrada.open("wb") as f:
            shutil.copyfileobj(archivo.file, f)

        historico = _cargar_historico()

        try:
            # El análisis tarda ~20 s con 80 mil filas: se corre en un hilo
            # para no bloquear el servidor.
            resultado = await run_in_threadpool(
                estadisticas_juicios.analizar,
                archivo=ruta_entrada,
                top_n=top_n,
                raps_por_trimestre=raps_por_trimestre,
                supuestos=supuestos_dict,
                historico=historico,
                fila_encabezado=fila_encabezado or None,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error procesando el archivo: {e}")

        # Excel descargable: usa los listados completos y luego se retiran del
        # resultado para no inflar la BD ni la respuesta al navegador.
        nombre_excel = f"estadisticas_juicios_{resultado['estadisticas']['resumen']['fecha_reporte']}.xlsx"
        try:
            ruta_excel = await run_in_threadpool(
                estadisticas_juicios_excel.generar_excel, resultado, carpeta_temporal / nombre_excel
            )
            ruta_storage = _subir_excel(ruta_excel, id_ejecucion)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"No se pudo generar el Excel: {e}")
        resultado.pop("_completo", None)

        parametros = {"top_n": top_n, "raps_por_trimestre": raps_por_trimestre, "archivo": archivo.filename, "supuestos": supuestos_dict}
        id_bd = guardar_ejecucion(
            modulo=NOMBRE_MODULO,
            parametros=parametros,
            resultado=resultado,
            archivos_generados={"EXCEL": ruta_storage},
        )

        return {
            "id_ejecucion": id_ejecucion,
            "id_bd": id_bd,
            "archivo_excel": nombre_excel,
            **resultado,
        }
    finally:
        shutil.rmtree(carpeta_temporal, ignore_errors=True)


@router.get("/descargar/{id_ejecucion}/{nombre_archivo}")
async def descargar_resultado(id_ejecucion: str, nombre_archivo: str):
    """Redirige a una URL firmada temporal del Excel en Supabase Storage."""
    ruta_storage = f"generados/{NOMBRE_MODULO}/{id_ejecucion}/{nombre_archivo}"
    url = supabase_storage.crear_url_firmada(ruta_storage, nombre_descarga=nombre_archivo)
    if not url:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return RedirectResponse(url)


@router.get("/historial")
async def historial(limite: int = 20):
    return listar_ejecuciones(modulo=NOMBRE_MODULO, limite=limite)