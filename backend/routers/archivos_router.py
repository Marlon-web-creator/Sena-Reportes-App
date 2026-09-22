import os
import re
import unicodedata
import uuid

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from auth.security import requiere_auth

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


def _sanear_nombre(nombre: str) -> str:
    """
    Deja un nombre de archivo SEGURO para usar como parte de una ruta
    dentro de Supabase Storage.

    Muchos nombres que llegan desde el navegador (con acentos, espacios,
    '#', '?', '%', paréntesis, etc.) hacen fallar silenciosamente el
    upload a Storage. Aquí se limpian sin tocar el nombre original que
    se guarda en la base de datos.
    """
    nombre = unicodedata.normalize("NFKD", nombre)
    nombre = "".join(c for c in nombre if not unicodedata.combining(c))

    nombre = nombre.replace(" ", "_")

    nombre = re.sub(r"[^A-Za-z0-9._-]+", "_", nombre)

    nombre = re.sub(r"_+", "_", nombre).strip("._")

    if len(nombre) > 80:
        raiz, _, ext = nombre.rpartition(".")
        nombre = f"{raiz[:75]}.{ext}" if ext else nombre[:80]

    return nombre or "archivo"


def _redirigir_a_url_firmada(ruta_storage: str, nombre_descarga: str | None = None) -> RedirectResponse:
    """
    Genera una URL firmada temporal para el archivo y redirige a ella.

    Nota: ya no pre-validamos con existe_archivo() (que lista la carpeta
    y puede dar falsos negativos si hay muchos archivos). En vez de eso,
    intentamos crear la URL firmada directamente: si Supabase confirma
    que el objeto no existe, crear_url_firmada() devuelve None y ahí sí
    respondemos 404.
    """
    if not ruta_storage:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    url = supabase_storage.crear_url_firmada(ruta_storage, nombre_descarga=nombre_descarga)

    if not url:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

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
# CARPETAS  (todas protegidas)
# ============================================================

@router.get("/carpetas", dependencies=[Depends(requiere_auth)])
def listar_carpetas_endpoint(padre_id: int | None = Query(None)):
    """
    Lista las subcarpetas directas de padre_id.
    Sin padre_id devuelve las carpetas de la raíz.
    """
    return database.listar_carpetas(padre_id)


@router.get("/carpetas/arbol", dependencies=[Depends(requiere_auth)])
def listar_arbol_carpetas():
    """
    Devuelve todas las carpetas (planas, con su padre_id), para construir
    un árbol completo o un selector de "mover a...".
    """
    return database.listar_todas_las_carpetas()


@router.get("/carpetas/{carpeta_id}/ruta", dependencies=[Depends(requiere_auth)])
def ruta_carpeta(carpeta_id: int):
    """
    Devuelve el breadcrumb (desde la raíz) hasta la carpeta indicada.
    """
    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    return database.obtener_ruta_carpeta(carpeta_id)


@router.get("/carpetas/{carpeta_id}/contenido", dependencies=[Depends(requiere_auth)])
def contenido_carpeta(carpeta_id: int):
    """
    Cuenta subcarpetas y archivos dentro de una carpeta (recursivamente).
    """
    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    return database.contar_contenido_carpeta(carpeta_id)


@router.post("/carpetas", dependencies=[Depends(requiere_auth)])
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


@router.put("/carpetas/{carpeta_id}", dependencies=[Depends(requiere_auth)])
def renombrar_carpeta_endpoint(carpeta_id: int, datos: CarpetaRenombrar):
    if not database.obtener_carpeta(carpeta_id):
        raise HTTPException(status_code=404, detail="Carpeta no encontrada")

    nombre = datos.nombre.strip()

    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre no puede estar vacío.")

    database.renombrar_carpeta(carpeta_id, nombre)

    return {"ok": True, "id": carpeta_id, "nombre": nombre}


@router.delete("/carpetas/{carpeta_id}", dependencies=[Depends(requiere_auth)])
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

# PROTEGIDO: el listado lo consume SOLO la página Base de Datos.
@router.get("/subidos", dependencies=[Depends(requiere_auth)])
def listar_subidos(carpeta_id: int | None = Query(None)):
    """
    Lista los archivos subidos dentro de una carpeta.
    Sin carpeta_id devuelve los archivos que están en la raíz.
    """
    return database.listar_archivos_subidos(carpeta_id)


async def _subir_un_archivo(
    archivo: UploadFile,
    modulo: str | None,
    carpeta_id_int: int | None,
) -> dict:
    """
    Sube un único archivo a Supabase Storage y registra la fila en la
    base de datos.
    """
    if not archivo.filename:
        return {
            "ok": False,
            "nombre_original": None,
            "error": "El archivo no tiene nombre.",
        }

    nombre_limpio = _sanear_nombre(archivo.filename)

    nombre_guardado = f"{uuid.uuid4().hex[:16]}_{nombre_limpio}"
    ruta_storage = f"uploads/{nombre_guardado}"

    try:
        contenido = await archivo.read()

        if not contenido:
            return {
                "ok": False,
                "nombre_original": archivo.filename,
                "error": "El archivo llegó vacío (0 bytes).",
            }

        supabase_storage.subir_bytes(
            ruta_storage,
            contenido,
            content_type=archivo.content_type or "application/octet-stream",
        )

        archivo_id = database.guardar_archivo_subido(
            nombre_original=archivo.filename,
            nombre_guardado=nombre_guardado,
            ruta=ruta_storage,
            tamano_bytes=len(contenido),
            modulo=modulo,
            carpeta_id=carpeta_id_int,
        )

        return {
            "ok": True,
            "id": archivo_id,
            "nombre_original": archivo.filename,
            "carpeta_id": carpeta_id_int,
        }

    except Exception as e:
        supabase_storage.eliminar_archivo(ruta_storage)

        return {
            "ok": False,
            "nombre_original": archivo.filename,
            "error": f"{type(e).__name__}: {e}",
        }


# ABIERTO: lo usan TODOS los módulos para subir sus archivos.
@router.post("/subidos")
async def subir_archivos(
    archivos: list[UploadFile] = File(...),
    modulo: str | None = Form(None),
    carpeta_id: str | None = Form(None),
):
    """
    Sube uno o varios archivos en una sola petición.
    """
    if not archivos:
        raise HTTPException(status_code=400, detail="No se recibió ningún archivo.")

    carpeta_id_int = _parsear_carpeta_id(carpeta_id)

    resultados = [
        await _subir_un_archivo(archivo, modulo, carpeta_id_int)
        for archivo in archivos
    ]

    subidos = [r for r in resultados if r["ok"]]
    fallidos = [r for r in resultados if not r["ok"]]

    return {
        "total": len(resultados),
        "total_subidos": len(subidos),
        "total_fallidos": len(fallidos),
        "subidos": subidos,
        "fallidos": fallidos,
    }


# PROTEGIDO: mover archivos es administración de la Base de Datos.
@router.put("/subidos/{archivo_id}/mover", dependencies=[Depends(requiere_auth)])
def mover_archivo_endpoint(archivo_id: int, datos: ArchivoMover):
    registro = database.obtener_archivo_subido(archivo_id)

    if not registro:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    if datos.carpeta_id is not None and not database.obtener_carpeta(datos.carpeta_id):
        raise HTTPException(status_code=404, detail="La carpeta destino no existe.")

    database.mover_archivo_a_carpeta(archivo_id, datos.carpeta_id)

    return {"ok": True, "id": archivo_id, "carpeta_id": datos.carpeta_id}


# ABIERTO: descarga por enlace directo (<a href>).
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


# PROTEGIDO: borrar un archivo es administración de la Base de Datos.
@router.delete("/subidos/{archivo_id}", dependencies=[Depends(requiere_auth)])
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

# PROTEGIDO: "Eliminar todo" es la acción más destructiva.
@router.delete("/subidos", dependencies=[Depends(requiere_auth)])
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

# PROTEGIDO: el listado lo consume SOLO la página Base de Datos.
@router.get("/generados", dependencies=[Depends(requiere_auth)])
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


# ABIERTO: descarga por enlace directo.
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


# PROTEGIDO: borrar es administración.
@router.delete("/generados/{ejecucion_id}/{clave}", dependencies=[Depends(requiere_auth)])
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

# PROTEGIDO.
@router.delete("/generados", dependencies=[Depends(requiere_auth)])
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