"""
supabase_storage.py

Reemplaza el uso del disco local (storage/uploads y storage/outputs) por
un bucket de Supabase Storage. Así los archivos persisten aunque Render
redespliegue o reinicie el servidor, sin necesidad de contratar disco.

Variables de entorno requeridas:
- SUPABASE_URL: URL de tu proyecto (Project Settings > API).
- SUPABASE_SERVICE_ROLE_KEY: la "service_role key" (NO la "anon key").
  Se usa desde el backend porque necesita poder leer/escribir/borrar sin
  pasar por Row Level Security. Nunca expongas esta key al frontend.
- SUPABASE_BUCKET (opcional, por defecto "archivos"): nombre del bucket
  de Storage. Créalo desde el dashboard de Supabase (puede ser privado).

Convención de rutas dentro del bucket:
- Archivos subidos manualmente:  uploads/<nombre_guardado>
- Archivos generados por los módulos: generados/<lo que ya use tu código>

Lo que antes era una ruta de disco (ej. "/opt/render/project/storage/
uploads/ab12_reporte.pdf") ahora es simplemente ese path relativo dentro
del bucket (ej. "uploads/ab12_reporte.pdf"). Esa es la cadena que se
guarda en la columna `ruta` de archivos_subidos y dentro del JSON
`archivos_generados` de las ejecuciones.
"""

import os
from functools import lru_cache

from supabase import create_client, Client


SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
BUCKET_NAME = os.environ.get("SUPABASE_BUCKET", "archivos")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "Faltan las variables de entorno SUPABASE_URL y/o "
        "SUPABASE_SERVICE_ROLE_KEY para conectar con Supabase Storage."
    )


@lru_cache(maxsize=1)
def get_client() -> Client:
    """
    Devuelve un cliente de Supabase reutilizable (se crea una sola vez
    por proceso).
    """
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def subir_bytes(ruta_storage: str, contenido: bytes, content_type: str | None = None) -> str:
    """
    Sube bytes al bucket de Supabase Storage en la ruta indicada.
    Devuelve la misma ruta_storage para que quede claro que fue guardada.
    """
    cliente = get_client()

    opciones = {}
    if content_type:
        opciones["content-type"] = content_type

    cliente.storage.from_(BUCKET_NAME).upload(
        path=ruta_storage,
        file=contenido,
        file_options=opciones or None,
    )

    return ruta_storage


def descargar_bytes(ruta_storage: str) -> bytes:
    """
    Descarga el contenido de un archivo del bucket. Lanza una excepción
    si no existe.
    """
    cliente = get_client()
    return cliente.storage.from_(BUCKET_NAME).download(ruta_storage)


def existe_archivo(ruta_storage: str) -> bool:
    """
    Comprueba si un archivo existe en el bucket, listando su carpeta
    contenedora (Supabase Storage no tiene un "exists" directo).
    """
    if not ruta_storage:
        return False

    cliente = get_client()
    carpeta = os.path.dirname(ruta_storage)
    nombre = os.path.basename(ruta_storage)

    try:
        items = cliente.storage.from_(BUCKET_NAME).list(carpeta or None)
    except Exception:
        return False

    return any(item.get("name") == nombre for item in items)


def eliminar_archivo(ruta_storage: str) -> bool:
    """
    Elimina un archivo del bucket. Devuelve True si no hubo error,
    False si algo falló (para que el caller decida si continuar,
    igual que antes se hacía con OSError).
    """
    if not ruta_storage:
        return False

    cliente = get_client()

    try:
        cliente.storage.from_(BUCKET_NAME).remove([ruta_storage])
        return True
    except Exception:
        return False


def crear_url_firmada(ruta_storage: str, nombre_descarga: str | None = None, expira_segundos: int = 300) -> str | None:
    """
    Genera una URL temporal (firmada) para descargar un archivo privado
    del bucket, válida por 'expira_segundos' (5 minutos por defecto).

    Si el archivo no existe, devuelve None.
    """
    cliente = get_client()

    opciones = {}
    if nombre_descarga:
        # Fuerza el nombre de archivo al descargar (soportado por
        # supabase-py >= 2.x). Si tu versión no lo soporta, quita esta
        # línea: el archivo igual se descarga, solo que con el nombre
        # que tenga en el bucket.
        opciones["download"] = nombre_descarga

    try:
        respuesta = cliente.storage.from_(BUCKET_NAME).create_signed_url(
            ruta_storage, expira_segundos, opciones or None
        )
    except Exception:
        return None

    # supabase-py devuelve distintas formas según la versión: puede ser
    # {"signedURL": "..."} o {"signedUrl": "..."}.
    return respuesta.get("signedURL") or respuesta.get("signedUrl")