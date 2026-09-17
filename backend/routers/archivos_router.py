import os
import uuid

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

import database
import supabase_storage


router = APIRouter(
    prefix="/api/archivos",
    tags=["archivos"],
)


def _parsear_carpeta_id(valor: str | None) -> int | None:
    """
    Convierte el valor de carpeta_id recibido por formulario (texto)
    a int|None, validando que exista si se proporciona.
    """
    if valor in (None, "", "null", "raiz", "raíz"):
        return None

    try:
        carpeta_id = int(valor)
    except ValueError:
        raise HTTPException(status_code=400, detail="carpeta_id inválido.")

    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="La carpeta indicada no existe.")

    return carpeta_id


def _redirigir_a_url_firmada(ruta_storage: str, nombre_descarga: str | None = None) -> RedirectResponse:
    """
    Genera una URL firmada temporal para el archivo y redirige a ella.
    Se usa en todos los endpoints de "descargar" en vez de servir el
    archivo directamente desde el servidor.
    """
    if not ruta_storage or not supabase_storage.existe_archivo(ruta_storage):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    url = supabase_storage.crear_url_firmada(ruta_storage, nombre_descarga=nombre_descarga)

    if not url:
        raise HTTPException(
            status_code=500,
            detail="No se pudo generar el enlace de descarga.",
        )

    return RedirectResponse(url)


# ============================================================
# MODELOS
# ============================================================

class CarpetaCrear(BaseModel):
    nombre: str
    padre_id: int | None = None


class CarpetaRenombrar(BaseModel):
    nombre: str


class ArchivoMover(BaseModel):
    carpeta_id: int | None = None


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
# CARPETAS
# ============================================================

@router.get("/carpetas")
def listar_carpetas_endpoint(padre_id: int | None = Query(None)):
    """
    Lista las subcarpetas directas de padre_id.
    Sin padre_id devuelve las carpetas de la raíz.
    """
    return database.listar_carpetas(padre_id)


@router.get("/carpetas/arbol")
def listar_arbol_carpetas():
    """
    Devuelve todas las carpetas (planas, con su padre_id), para construir
    un árbol completo o un selector de "mover a...".
    """
    return database.listar_todas_las_carpetas()


@router.get("/carpetas/{carpeta_id}/ruta")
def ruta_carpeta(carpeta_id: int):
    """
    Devuelve el breadcrumb (desde la raíz) hasta la carpeta indicada.
    """
    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    return database.obtener_ruta_carpeta(carpeta_id)


@router.get("/carpetas/{carpeta_id}/contenido")
def contenido_carpeta(carpeta_id: int):
    """
    Cuenta subcarpetas y archivos dentro de una carpeta (recursivamente).
    Útil para confirmar antes de eliminarla.
    """
    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    return database.contar_contenido_carpeta(carpeta_id)


@router.post("/carpetas")
def crear_carpeta_endpoint(datos: CarpetaCrear):
    nombre = datos.nombre.strip()

    if not nombre:
        raise HTTPException(
            status_code=400,
            detail="El nombre de la carpeta no puede estar vacío.",
        )

    if datos.padre_id is not None and not database.obtener_carpeta(datos.padre_id):
        raise HTTPException(status_code=404, detail="La carpeta padre no existe.")

    carpeta_id = database.crear_carpeta(nombre, datos.padre_id)

    return {
        "id": carpeta_id,
        "nombre": nombre,
        "padre_id": datos.padre_id,
    }


@router.put("/carpetas/{carpeta_id}")
def renombrar_carpeta_endpoint(carpeta_id: int, datos: CarpetaRenombrar):
    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    nombre = datos.nombre.strip()

    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre no puede estar vacío.")

    database.renombrar_carpeta(carpeta_id, nombre)

    return {"ok": True, "id": carpeta_id, "nombre": nombre}


@router.delete("/carpetas/{carpeta_id}")
def eliminar_carpeta_endpoint(carpeta_id: int):
    """
    Elimina una carpeta, sus subcarpetas y los archivos que contienen
    (registro en BD + archivo físico en Supabase Storage).
    """
    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    registros = database.eliminar_carpeta(carpeta_id)

    archivos_borrados = 0

    for registro in registros:
        if supabase_storage.eliminar_archivo(registro.get("ruta")):
            archivos_borrados += 1

    return {
        "ok": True,
        "archivos_eliminados": len(registros),
        "archivos_borrados_disco": archivos_borrados,
    }


# ============================================================
# ARCHIVOS SUBIDOS
# ============================================================

@router.get("/subidos")
def listar_subidos(carpeta_id: int | None = Query(None)):
    """
    Lista los archivos subidos dentro de una carpeta.
    Sin carpeta_id devuelve los archivos que están en la raíz.
    """
    return database.listar_archivos_subidos(carpeta_id)


@router.post("/subidos")
async def subir_archivo(
    archivo: UploadFile = File(...),
    modulo: str | None = Form(None),
    carpeta_id: str | None = Form(None),
):
    if not archivo.filename:
        raise HTTPException(
            status_code=400,
            detail="El archivo no tiene nombre.",
        )

    carpeta_id_int = _parsear_carpeta_id(carpeta_id)

    nombre_guardado = f"{uuid.uuid4().hex[:10]}_{archivo.filename}"
    ruta_storage = f"uploads/{nombre_guardado}"

    try:
        contenido = await archivo.read()

        supabase_storage.subir_bytes(
            ruta_storage,
            contenido,
            content_type=archivo.content_type,
        )

        archivo_id = database.guardar_archivo_subido(
            nombre_original=archivo.filename,
            nombre_guardado=nombre_guardado,
            ruta=ruta_storage,
            tamano_bytes=len(contenido),
            modulo=modulo,
            carpeta_id=carpeta_id_int,
        )

    except HTTPException:
        raise

    except Exception as e:
        # Si algo falla después de subir el archivo a Storage,
        # intentamos limpiarlo para no dejar huérfanos.
        supabase_storage.eliminar_archivo(ruta_storage)

        raise HTTPException(
            status_code=500,
            detail=f"No se pudo guardar el archivo: {e}",
        )

    return {
        "id": archivo_id,
        "nombre_original": archivo.filename,
        "carpeta_id": carpeta_id_int,
    }


@router.put("/subidos/{archivo_id}/mover")
def mover_archivo_endpoint(archivo_id: int, datos: ArchivoMover):
    registro = database.obtener_archivo_subido(archivo_id)

    if not registro:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    if datos.carpeta_id is not None and not database.obtener_carpeta(datos.carpeta_id):
        raise HTTPException(status_code=404, detail="La carpeta destino no existe.")

    database.mover_archivo_a_carpeta(archivo_id, datos.carpeta_id)

    return {"ok": True, "id": archivo_id, "carpeta_id": datos.carpeta_id}


@router.get("/subidos/{archivo_id}/descargar")
def descargar_subido(archivo_id: int):
    registro = database.obtener_archivo_subido(archivo_id)

    if not registro:
        raise HTTPException(
            status_code=404,
            detail="Archivo no encontrado",
        )

    return _redirigir_a_url_firmada(
        registro.get("ruta"),
        nombre_descarga=registro.get("nombre_original"),
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

    if ruta and supabase_storage.existe_archivo(ruta):
        if not supabase_storage.eliminar_archivo(ruta):
            raise HTTPException(
                status_code=500,
                detail="No se pudo eliminar el archivo físico en Supabase Storage.",
            )

    database.eliminar_archivo_subido(archivo_id)

    return {
        "ok": True,
        "id": archivo_id,
    }


# ============================================================
# ELIMINAR TODOS LOS ARCHIVOS SUBIDOS (Y CARPETAS)
# ============================================================

@router.delete("/subidos")
def eliminar_todos_subidos():
    """
    Elimina todos los registros de archivos_subidos, todas las carpetas,
    y los archivos físicos asociados en Supabase Storage.
    """
    registros = database.vaciar_archivos_subidos()

    archivos_borrados = 0

    for registro in registros:
        if supabase_storage.eliminar_archivo(registro.get("ruta")):
            archivos_borrados += 1

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
                    "existe": supabase_storage.existe_archivo(ruta),
                }
            )

    return items


@router.get("/generados/{ejecucion_id}/{clave}/descargar")
def descargar_generado(ejecucion_id: int, clave: str):
    ejecucion = database.obtener_ejecucion(ejecucion_id)

    if not ejecucion:
        raise HTTPException(
            status_code=404,
            detail="Ejecución no encontrada",
        )

    archivos_generados = ejecucion.get("archivos_generados") or {}
    ruta = archivos_generados.get(clave)

    return _redirigir_a_url_firmada(ruta, nombre_descarga=os.path.basename(ruta) if ruta else None)


@router.delete("/generados/{ejecucion_id}/{clave}")
def eliminar_generado(ejecucion_id: int, clave: str):
    ejecucion = database.obtener_ejecucion(ejecucion_id)

    if not ejecucion:
        raise HTTPException(
            status_code=404,
            detail="Ejecución no encontrada",
        )

    archivos_generados = ejecucion.get("archivos_generados") or {}
    ruta = archivos_generados.get(clave)

    if ruta and supabase_storage.existe_archivo(ruta):
        if not supabase_storage.eliminar_archivo(ruta):
            raise HTTPException(
                status_code=500,
                detail="No se pudo eliminar el archivo físico en Supabase Storage.",
            )

    database.quitar_archivo_generado(ejecucion_id, clave)

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
    Elimina todos los archivos generados físicamente (en Supabase Storage)
    y limpia su registro en la base de datos.
    """
    rutas = database.vaciar_archivos_generados()

    archivos_borrados = 0

    for ruta in rutas:
        if supabase_storage.eliminar_archivo(ruta):
            archivos_borrados += 1

    return {
        "ok": True,
        "registros_eliminados": len(rutas),
        "archivos_borrados": archivos_borrados,
    }