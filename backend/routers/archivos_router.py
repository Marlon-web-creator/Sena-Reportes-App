import os
import uuid
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

import database


router = APIRouter(
    prefix="/api/archivos",
    tags=["archivos"],
)


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "storage" / "uploads"
OUTPUTS_DIR = BASE_DIR / "storage" / "outputs"

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# DIAGNÓSTICO
# ============================================================

@router.get("/_rutas")
def listar_rutas():
    """
    Muestra las rutas registradas dentro de este router.
    Útil para comprobar que FastAPI está cargando este archivo.
    """
    return [
        {
            "path": r.path,
            "methods": sorted(r.methods or []),
        }
        for r in router.routes
    ]


# ============================================================
# ARCHIVOS SUBIDOS
# ============================================================

@router.get("/subidos")
def listar_subidos():
    return database.listar_archivos_subidos()


@router.post("/subidos")
async def subir_archivo(
    archivo: UploadFile = File(...),
    modulo: str | None = Form(None),
):
    if not archivo.filename:
        raise HTTPException(
            status_code=400,
            detail="El archivo no tiene nombre.",
        )

    nombre_guardado = f"{uuid.uuid4().hex[:10]}_{archivo.filename}"
    destino = UPLOADS_DIR / nombre_guardado

    try:
        contenido = await archivo.read()
        destino.write_bytes(contenido)

        archivo_id = database.guardar_archivo_subido(
            nombre_original=archivo.filename,
            nombre_guardado=nombre_guardado,
            ruta=str(destino),
            tamano_bytes=len(contenido),
            modulo=modulo,
        )

    except Exception as e:
        # Si algo falla después de crear el archivo físico,
        # intentamos limpiarlo.
        if destino.exists():
            try:
                destino.unlink()
            except OSError:
                pass

        raise HTTPException(
            status_code=500,
            detail=f"No se pudo guardar el archivo: {e}",
        )

    return {
        "id": archivo_id,
        "nombre_original": archivo.filename,
    }


@router.get("/subidos/{archivo_id}/descargar")
def descargar_subido(archivo_id: int):
    registro = database.obtener_archivo_subido(archivo_id)

    if not registro:
        raise HTTPException(
            status_code=404,
            detail="Archivo no encontrado",
        )

    ruta = registro.get("ruta")

    if not ruta or not os.path.exists(ruta):
        raise HTTPException(
            status_code=404,
            detail="Archivo físico no encontrado",
        )

    return FileResponse(
        ruta,
        filename=registro["nombre_original"],
    )


@router.delete("/subidos/{archivo_id}")
def eliminar_subido(archivo_id: int):
    registro = database.obtener_archivo_subido(archivo_id)

    if not registro:
        raise HTTPException(
            status_code=404,
            detail="Archivo no encontrado",
        )

    ruta = registro.get("ruta")

    if ruta and os.path.exists(ruta):
        try:
            os.remove(ruta)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"No se pudo eliminar el archivo físico: {e}",
            )

    database.eliminar_archivo_subido(archivo_id)

    return {
        "ok": True,
        "id": archivo_id,
    }


# ============================================================
# ELIMINAR TODOS LOS ARCHIVOS SUBIDOS
# ============================================================

@router.delete("/subidos")
def eliminar_todos_subidos():
    """
    Elimina todos los registros de archivos_subidos
    y sus archivos físicos asociados.
    """

    registros = database.vaciar_archivos_subidos()

    archivos_borrados = 0

    for registro in registros:
        ruta = registro.get("ruta")

        if not ruta or not os.path.exists(ruta):
            continue

        try:
            os.remove(ruta)
            archivos_borrados += 1
        except OSError:
            # Un archivo que no se pueda borrar no debe impedir
            # que se eliminen los demás registros.
            pass

    return {
        "ok": True,
        "registros_eliminados": len(registros),
        "archivos_borrados": archivos_borrados,
    }


# ============================================================
# ARCHIVOS GENERADOS
# ============================================================

@router.get("/generados")
def listar_generados():
    ejecuciones = database.listar_ejecuciones(limite=200)

    items = []

    for ejecucion in ejecuciones:
        archivos_generados = ejecucion.get("archivos_generados") or {}

        for clave, ruta in archivos_generados.items():
            items.append(
                {
                    "ejecucion_id": ejecucion["id"],
                    "modulo": ejecucion["modulo"],
                    "fecha": ejecucion["fecha"],
                    "clave": clave,
                    "ruta": ruta,
                    "nombre_archivo": os.path.basename(ruta),
                    "existe": os.path.exists(ruta),
                }
            )

    return items


@router.get("/generados/{ejecucion_id}/{clave}/descargar")
def descargar_generado(
    ejecucion_id: int,
    clave: str,
):
    ejecucion = database.obtener_ejecucion(ejecucion_id)

    if not ejecucion:
        raise HTTPException(
            status_code=404,
            detail="Ejecución no encontrada",
        )

    archivos_generados = ejecucion.get("archivos_generados") or {}
    ruta = archivos_generados.get(clave)

    if not ruta or not os.path.exists(ruta):
        raise HTTPException(
            status_code=404,
            detail="Archivo no encontrado",
        )

    return FileResponse(
        ruta,
        filename=os.path.basename(ruta),
    )


@router.delete("/generados/{ejecucion_id}/{clave}")
def eliminar_generado(
    ejecucion_id: int,
    clave: str,
):
    ejecucion = database.obtener_ejecucion(ejecucion_id)

    if not ejecucion:
        raise HTTPException(
            status_code=404,
            detail="Ejecución no encontrada",
        )

    archivos_generados = ejecucion.get("archivos_generados") or {}
    ruta = archivos_generados.get(clave)

    if ruta and os.path.exists(ruta):
        try:
            os.remove(ruta)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"No se pudo eliminar el archivo físico: {e}",
            )

    database.quitar_archivo_generado(
        ejecucion_id,
        clave,
    )

    return {
        "ok": True,
        "ejecucion_id": ejecucion_id,
        "clave": clave,
    }


# ============================================================
# ELIMINAR TODOS LOS ARCHIVOS GENERADOS
# ============================================================

@router.delete("/generados")
def eliminar_todos_generados():
    """
    Elimina todos los archivos generados físicamente
    y limpia su registro en la base de datos.
    """

    rutas = database.vaciar_archivos_generados()

    archivos_borrados = 0

    for ruta in rutas:
        if not ruta or not os.path.exists(ruta):
            continue

        try:
            os.remove(ruta)
            archivos_borrados += 1
        except OSError:
            # Continuamos con los demás archivos.
            pass

    return {
        "ok": True,
        "registros_eliminados": len(rutas),
        "archivos_borrados": archivos_borrados,
    }

