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

Variables de entorno opcionales (para timeouts):
- SUPABASE_TIMEOUT_CONNECT (por defecto 10s): timeout de conexión TCP.
- SUPABASE_TIMEOUT_READ    (por defecto 60s): timeout de lectura.
- SUPABASE_TIMEOUT_WRITE   (por defecto 120s): timeout de escritura.
- SUPABASE_TIMEOUT_POOL    (por defecto 10s): timeout de espera de pool.

Convención de rutas dentro del bucket:
- Archivos subidos manualmente:  uploads/<nombre_guardado>
- Archivos generados por los módulos: generados/<lo que ya use tu código>

Lo que antes era una ruta de disco (ej. "/opt/render/project/storage/
uploads/ab12_reporte.pdf") ahora es simplemente ese path relativo dentro
del bucket (ej. "uploads/ab12_reporte.pdf"). Esa es la cadena que se
guarda en la columna `ruta` de archivos_subidos y dentro del JSON
`archivos_generados` de las ejecuciones.
"""

import logging
import os
import threading
from functools import lru_cache
from urllib.parse import quote

import httpx
from supabase import create_client, Client

# ============================================================
# LOGGING
# ============================================================
logger = logging.getLogger("supabase_storage")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] supabase_storage: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(_h)
    logger.propagate = False


# ============================================================
# CONFIGURACIÓN
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
BUCKET_NAME = os.environ.get("SUPABASE_BUCKET", "archivos")

# Timeouts (segundos). Se pueden ajustar por env vars en Render.
TIMEOUT_CONNECT = float(os.environ.get("SUPABASE_TIMEOUT_CONNECT") or 10)
TIMEOUT_READ    = float(os.environ.get("SUPABASE_TIMEOUT_READ")    or 60)
TIMEOUT_WRITE   = float(os.environ.get("SUPABASE_TIMEOUT_WRITE")   or 120)
TIMEOUT_POOL    = float(os.environ.get("SUPABASE_TIMEOUT_POOL")    or 10)

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "Faltan las variables de entorno SUPABASE_URL y/o "
        "SUPABASE_SERVICE_ROLE_KEY para conectar con Supabase Storage."
    )


# ============================================================
# CLIENTE httpx REUTILIZABLE (thread-safe)
# ============================================================
_http_client: httpx.Client | None = None
_http_lock = threading.Lock()


def _get_http_client() -> httpx.Client:
    """
    Devuelve un cliente httpx compartido (con connection pool). Se crea
    una sola vez por proceso. Es thread-safe: se puede usar desde varios
    hilos a la vez sin problema.
    """
    global _http_client
    if _http_client is None:
        with _http_lock:
            if _http_client is None:
                _http_client = httpx.Client(
                    timeout=httpx.Timeout(
                        connect=TIMEOUT_CONNECT,
                        read=TIMEOUT_READ,
                        write=TIMEOUT_WRITE,
                        pool=TIMEOUT_POOL,
                    ),
                    limits=httpx.Limits(
                        max_keepalive_connections=20,
                        max_connections=50,
                    ),
                    follow_redirects=True,
                )
                logger.info(
                    "httpx.Client creado: connect=%.0fs read=%.0fs write=%.0fs pool=%.0fs",
                    TIMEOUT_CONNECT, TIMEOUT_READ, TIMEOUT_WRITE, TIMEOUT_POOL,
                )
    return _http_client


def _auth_headers(extra: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }
    if extra:
        headers.update(extra)
    return headers


def _url_storage(ruta_storage: str) -> str:
    """
    Construye la URL REST de Storage para un objeto.
    Ej: https://xxx.supabase.co/storage/v1/object/archivos/uploads/a.pdf
    """
    # Codificamos cada segmento pero dejamos los '/' para que las
    # subcarpetas sigan funcionando.
    path_codificado = quote(ruta_storage, safe="/")
    return f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{path_codificado}"


# ============================================================
# CLIENTE supabase-py (para operaciones ligeras)
# ============================================================
@lru_cache(maxsize=1)
def get_client() -> Client:
    """
    Devuelve un cliente de Supabase reutilizable (se crea una sola vez
    por proceso). Se usa para operaciones ligeras (list, remove,
    create_signed_url) donde el timeout no es crítico.
    """
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ============================================================
# SUBIR / DESCARGAR (con timeout, vía httpx directo)
# ============================================================

def subir_bytes(ruta_storage: str, contenido: bytes,
                content_type: str | None = None) -> str:
    """
    Sube bytes al bucket de Supabase Storage en la ruta indicada.
    Usa httpx con timeout de escritura, así una red lenta no deja el
    hilo colgado para siempre.

    Devuelve la misma ruta_storage para que quede claro que fue guardada.

    Nota: por defecto Supabase Storage devuelve 409 si el archivo ya
    existe. Aquí hacemos "upsert": si existe, lo sobreescribimos.
    """
    url = _url_storage(ruta_storage)
    headers = _auth_headers({
        "Content-Type": content_type or "application/octet-stream",
        "x-upsert": "true",
    })

    t0 = time.time() if (time := __import__("time")) else 0  # noqa
    # (dejamos la línea simple de abajo; la de arriba era un error mío)
    import time as _time
    t0 = _time.time()

    cliente = _get_http_client()
    resp = cliente.post(url, content=contenido, headers=headers)

    # Si tu versión de Storage ignora `x-upsert`, probamos el fallback.
    if resp.status_code == 409:
        logger.info("Archivo ya existe, borrando y re-subiendo: %s", ruta_storage)
        eliminar_archivo(ruta_storage)
        resp = cliente.post(url, content=contenido, headers=headers)

    resp.raise_for_status()
    logger.info(
        "subir_bytes OK: %s (%d bytes en %.2fs)",
        ruta_storage, len(contenido), _time.time() - t0,
    )
    return ruta_storage


def descargar_bytes(ruta_storage: str) -> bytes:
    """
    Descarga el contenido de un archivo del bucket. Usa httpx con
    timeout de lectura, así una conexión colgada NO bloquea el hilo
    para siempre.

    Lanza httpx.HTTPStatusError si el archivo no existe (404).
    """
    url = _url_storage(ruta_storage)
    headers = _auth_headers()

    import time as _time
    t0 = _time.time()
    cliente = _get_http_client()
    resp = cliente.get(url, headers=headers)
    resp.raise_for_status()

    datos = resp.content
    dur = _time.time() - t0
    if dur > 3:
        logger.warning(
            "descargar_bytes LENTO: %s (%d bytes en %.1fs)",
            ruta_storage, len(datos), dur,
        )
    return datos


# ============================================================
# OPERACIONES LIGERAS (supabase-py)
# ============================================================

def existe_archivo(ruta_storage: str) -> bool:
    """
    Comprueba si un archivo existe en el bucket, listando su carpeta
    contenedora (Supabase Storage no tiene un "exists" directo).

    Usamos el parámetro "search" para que el filtrado lo haga Supabase
    del lado del servidor: así no dependemos de que el archivo caiga
    dentro de la primera página de resultados. Sin "search", list()
    devuelve por defecto solo 100 objetos, y con carpetas que ya
    acumulan cientos de archivos (como "uploads/") eso provocaba falsos
    negativos: el archivo SÍ existía en el bucket, pero como no estaba
    en esos primeros 100 resultados, existe_archivo() devolvía False y
    la descarga fallaba con 404 aunque el archivo estuviera ahí.
    """
    if not ruta_storage:
        return False

    cliente = get_client()
    carpeta = os.path.dirname(ruta_storage)
    nombre = os.path.basename(ruta_storage)

    try:
        items = cliente.storage.from_(BUCKET_NAME).list(
            carpeta or None,
            {"search": nombre, "limit": 10},
        )
    except Exception:
        logger.exception("Error listando carpeta %s", carpeta)
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
        logger.exception("Error eliminando %s", ruta_storage)
        return False


def crear_url_firmada(ruta_storage: str, nombre_descarga: str | None = None,
                      expira_segundos: int = 300) -> str | None:
    """
    Genera una URL temporal (firmada) para descargar un archivo privado
    del bucket, válida por 'expira_segundos' (5 minutos por defecto).

    Si el archivo no existe, devuelve None.
    """
    cliente = get_client()

    opciones = {}
    if nombre_descarga:
        opciones["download"] = nombre_descarga

    try:
        respuesta = cliente.storage.from_(BUCKET_NAME).create_signed_url(
            ruta_storage, expira_segundos, opciones or None
        )
    except Exception:
        logger.exception("Error creando URL firmada para %s", ruta_storage)
        return None

    # supabase-py devuelve distintas formas según la versión:
    # {"signedURL": "..."} o {"signedUrl": "..."}.
    return respuesta.get("signedURL") or respuesta.get("signedUrl")