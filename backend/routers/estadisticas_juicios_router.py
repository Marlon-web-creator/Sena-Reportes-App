"""
routers/estadisticas_juicios_router.py

Endpoints del módulo "Estadísticas y proyección de juicios".

Mismo patrón que depuracion_router.py: el archivo subido vive en una
carpeta temporal que se borra al terminar la petición. Este módulo no
genera archivo de salida; el JSON resultante se guarda en BD (historial)
y alimenta la proyección histórica de las ejecuciones siguientes.
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

from database import guardar_ejecucion, listar_ejecuciones
from modules import estadisticas_juicios

router = APIRouter(prefix="/api/estadisticas-juicios", tags=["Estadísticas de juicios"])

EXTENSIONES_PERMITIDAS = {".xlsx"}
NOMBRE_MODULO = "estadisticas_juicios"


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
    limite: int = Form(20),
    top_n: int = Form(10),
    # Opcional: JSON con supuestos, p. ej. {"base": {"con_sync": 0.8}}
    supuestos: Optional[str] = Form(None),
    fila_encabezado: Optional[int] = Form(None),
):
    if Path(archivo.filename).suffix.lower() not in EXTENSIONES_PERMITIDAS:
        raise HTTPException(status_code=400, detail="El archivo debe ser .xlsx.")
    if limite < 0 or not (1 <= top_n <= 50):
        raise HTTPException(status_code=400, detail="Parámetros fuera de rango (límite >= 0, top entre 1 y 50).")

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
                limite=limite,
                top_n=top_n,
                supuestos=supuestos_dict,
                historico=historico,
                fila_encabezado=fila_encabezado or None,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error procesando el archivo: {e}")

        parametros = {"limite": limite, "top_n": top_n, "archivo": archivo.filename, "supuestos": supuestos_dict}
        id_bd = guardar_ejecucion(
            modulo=NOMBRE_MODULO,
            parametros=parametros,
            resultado=resultado,
            archivos_generados={},
        )

        return {"id_ejecucion": id_ejecucion, "id_bd": id_bd, **resultado}
    finally:
        shutil.rmtree(carpeta_temporal, ignore_errors=True)


@router.get("/historial")
async def historial(limite: int = 20):
    return listar_ejecuciones(modulo=NOMBRE_MODULO, limite=limite)
