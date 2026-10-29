"""
database.py

Conexión PostgreSQL (Supabase) para guardar:
- Historial de ejecuciones de los módulos.
- Archivos subidos manualmente desde la sección "Base de Datos".
- Carpetas y subcarpetas para organizar esos archivos.

IMPORTANTE - Migración desde SQLite:
Antes esto usaba un archivo app.db local (que se perdía al redesplegar en
Render sin disco persistente). Ahora usa la base de datos Postgres que
provee Supabase, que persiste sin necesidad de contratar disco en Render.

La columna "ruta" de archivos_subidos y las rutas guardadas dentro de
archivos_generados ya NO son rutas del sistema de archivos local: ahora
son "paths" dentro de un bucket de Supabase Storage (ver supabase_storage.py).

Variables de entorno requeridas:
- DATABASE_URL: cadena de conexión de Postgres que te da Supabase.
  Project Settings > Database > Connection string.
  Se recomienda usar la del "Connection pooler" (modo transaction,
  puerto 6543) para no agotar conexiones, ej:
  postgresql://postgres.xxxx:TU_PASSWORD@aws-0-us-east-1.pooler.supabase.com:6543/postgres

Notas:
- Todas las fechas se guardan en hora de Colombia (America/Bogota, UTC-5),
  formateadas como d/m/aaaa hh:mm:ss (ej. 2/12/2026 14:30:05), igual que antes.
- parametros / resultado / archivos_generados ahora son columnas JSONB:
  psycopg2 las entrega ya como dict/list de Python (no hace falta
  json.loads), y para insertarlas se envuelven con psycopg2.extras.Json(...).
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "Falta la variable de entorno DATABASE_URL con la cadena de "
        "conexión de Postgres de Supabase."
    )

# Colombia no tiene horario de verano: America/Bogota es siempre UTC-5.
ZONA_BOGOTA = ZoneInfo("America/Bogota")


def _ahora_bogota() -> str:
    """
    Devuelve la fecha y hora actual en zona horaria de Bogotá,
    formateada como d/m/aaaa hh:mm:ss (sin ceros a la izquierda en día/mes).
    """
    ahora = datetime.now(ZONA_BOGOTA)
    return f"{ahora.day}/{ahora.month}/{ahora.year} {ahora.strftime('%H:%M:%S')}"


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------

def get_connection():
    """
    Crea y devuelve una conexión a Postgres (Supabase).

    Se usa RealDictCursor para que las filas se comporten como dict,
    igual que sqlite3.Row antes (fila["columna"]).
    """
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


# ---------------------------------------------------------------------------
# Migraciones ligeras (para bases de datos ya existentes)
# ---------------------------------------------------------------------------

def _migrar_columnas_faltantes(conn) -> None:
    """
    Agrega columnas nuevas a tablas que ya existían antes de introducir
    carpetas, sin romper instalaciones previas.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'archivos_subidos'
            """
        )
        columnas = {fila["column_name"] for fila in cur.fetchall()}

        if "carpeta_id" not in columnas:
            cur.execute(
                "ALTER TABLE archivos_subidos ADD COLUMN carpeta_id INTEGER "
                "REFERENCES carpetas(id)"
            )


# ---------------------------------------------------------------------------
# Inicialización de la base de datos
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Crea las tablas necesarias si todavía no existen.
    Llamar una vez al arrancar la app (igual que antes).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # -----------------------------------------------------------
            # Historial de ejecuciones
            # -----------------------------------------------------------
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ejecuciones (
                    id SERIAL PRIMARY KEY,
                    modulo TEXT NOT NULL,
                    fecha TEXT NOT NULL,
                    parametros JSONB,
                    resultado JSONB,
                    archivos_generados JSONB
                )
                """
            )

            # -----------------------------------------------------------
            # Carpetas (deben crearse antes que archivos_subidos por la FK)
            # -----------------------------------------------------------
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS carpetas (
                    id SERIAL PRIMARY KEY,
                    nombre TEXT NOT NULL,
                    padre_id INTEGER REFERENCES carpetas(id),
                    fecha_creacion TEXT NOT NULL
                )
                """
            )

            # -----------------------------------------------------------
            # Archivos subidos manualmente
            # -----------------------------------------------------------
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS archivos_subidos (
                    id SERIAL PRIMARY KEY,
                    nombre_original TEXT NOT NULL,
                    nombre_guardado TEXT NOT NULL,
                    ruta TEXT NOT NULL,
                    tamano_bytes INTEGER,
                    modulo TEXT,
                    carpeta_id INTEGER REFERENCES carpetas(id),
                    fecha_subida TEXT NOT NULL
                )
                """
            )

        _migrar_columnas_faltantes(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Historial de ejecuciones
# ---------------------------------------------------------------------------

def guardar_ejecucion(
    modulo: str,
    parametros: dict,
    resultado: dict,
    archivos_generados: dict,
) -> int:
    """
    Guarda una ejecución en el historial y devuelve su ID.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ejecuciones (
                    modulo, fecha, parametros, resultado, archivos_generados
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    modulo,
                    _ahora_bogota(),
                    psycopg2.extras.Json(parametros),
                    psycopg2.extras.Json(resultado),
                    psycopg2.extras.Json(archivos_generados),
                ),
            )
            nuevo_id = cur.fetchone()["id"]
        conn.commit()
        return nuevo_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def listar_ejecuciones(
    modulo: str | None = None,
    limite: int = 20,
) -> list[dict]:
    """
    Devuelve las ejecuciones más recientes.

    Si se proporciona modulo, solamente devuelve las ejecuciones
    pertenecientes a ese módulo.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if modulo:
                cur.execute(
                    """
                    SELECT * FROM ejecuciones
                    WHERE modulo = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (modulo, limite),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM ejecuciones
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (limite,),
                )
            filas = cur.fetchall()
    finally:
        conn.close()

    resultado = []
    for fila in filas:
        item = dict(fila)
        # JSONB ya llega como dict/list; solo cubrimos el caso NULL.
        item["parametros"] = item["parametros"] or {}
        item["resultado"] = item["resultado"] or {}
        item["archivos_generados"] = item["archivos_generados"] or {}
        resultado.append(item)

    return resultado


def obtener_ejecucion(ejecucion_id: int) -> dict | None:
    """
    Obtiene una ejecución específica por su ID.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ejecuciones WHERE id = %s", (ejecucion_id,))
            fila = cur.fetchone()
    finally:
        conn.close()

    if not fila:
        return None

    item = dict(fila)
    item["parametros"] = item["parametros"] or {}
    item["resultado"] = item["resultado"] or {}
    item["archivos_generados"] = item["archivos_generados"] or {}
    return item


def quitar_archivo_generado(ejecucion_id: int, clave: str) -> None:
    """
    Elimina una entrada concreta del JSON de archivos_generados
    de una ejecución.

    Ejemplo:
        quitar_archivo_generado(15, "RAMOS")
    """
    ejecucion = obtener_ejecucion(ejecucion_id)

    if not ejecucion:
        return

    archivos = ejecucion["archivos_generados"]
    archivos.pop(clave, None)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ejecuciones SET archivos_generados = %s WHERE id = %s",
                (psycopg2.extras.Json(archivos), ejecucion_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Carpetas
# ---------------------------------------------------------------------------

def crear_carpeta(nombre: str, padre_id: int | None = None) -> int:
    """
    Crea una carpeta (opcionalmente dentro de otra) y devuelve su ID.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO carpetas (nombre, padre_id, fecha_creacion)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (nombre.strip(), padre_id, _ahora_bogota()),
            )
            nuevo_id = cur.fetchone()["id"]
        conn.commit()
        return nuevo_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def obtener_carpeta(carpeta_id: int) -> dict | None:
    """
    Obtiene una carpeta por su ID.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM carpetas WHERE id = %s", (carpeta_id,))
            fila = cur.fetchone()
    finally:
        conn.close()

    return dict(fila) if fila else None


def listar_carpetas(padre_id: int | None = None) -> list[dict]:
    """
    Lista las subcarpetas directas de padre_id.
    padre_id=None devuelve las carpetas de la raíz.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if padre_id is None:
                cur.execute(
                    """
                    SELECT * FROM carpetas
                    WHERE padre_id IS NULL
                    ORDER BY nombre COLLATE "C"
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM carpetas
                    WHERE padre_id = %s
                    ORDER BY nombre COLLATE "C"
                    """,
                    (padre_id,),
                )
            filas = cur.fetchall()
    finally:
        conn.close()

    return [dict(f) for f in filas]


def listar_todas_las_carpetas() -> list[dict]:
    """
    Devuelve todas las carpetas (planas, con su padre_id), útil para
    construir un árbol completo o un selector de "mover a...".
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM carpetas ORDER BY nombre COLLATE "C"')
            filas = cur.fetchall()
    finally:
        conn.close()

    return [dict(f) for f in filas]


def obtener_ruta_carpeta(carpeta_id: int | None) -> list[dict]:
    """
    Devuelve la ruta (breadcrumb) desde la raíz hasta carpeta_id,
    como lista de {id, nombre}, empezando por la raíz.
    """
    if carpeta_id is None:
        return []

    ruta: list[dict] = []
    actual_id: int | None = carpeta_id
    visitados: set[int] = set()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            while actual_id is not None:
                if actual_id in visitados:
                    break  # protección ante ciclos accidentales
                visitados.add(actual_id)

                cur.execute(
                    "SELECT id, nombre, padre_id FROM carpetas WHERE id = %s",
                    (actual_id,),
                )
                fila = cur.fetchone()

                if not fila:
                    break

                ruta.insert(0, {"id": fila["id"], "nombre": fila["nombre"]})
                actual_id = fila["padre_id"]
    finally:
        conn.close()

    return ruta


def renombrar_carpeta(carpeta_id: int, nuevo_nombre: str) -> None:
    """
    Cambia el nombre de una carpeta.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE carpetas SET nombre = %s WHERE id = %s",
                (nuevo_nombre.strip(), carpeta_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ids_descendientes(conn, carpeta_id: int) -> list[int]:
    """
    BFS: devuelve carpeta_id y los IDs de todas sus subcarpetas,
    recursivamente.
    """
    ids = [carpeta_id]
    pendientes = [carpeta_id]

    with conn.cursor() as cur:
        while pendientes:
            actual = pendientes.pop()
            cur.execute("SELECT id FROM carpetas WHERE padre_id = %s", (actual,))
            hijos = cur.fetchall()

            for hijo in hijos:
                ids.append(hijo["id"])
                pendientes.append(hijo["id"])

    return ids


def contar_contenido_carpeta(carpeta_id: int) -> dict:
    """
    Cuenta cuántas subcarpetas y archivos hay dentro de una carpeta
    (incluyendo subcarpetas anidadas). Útil para confirmar un borrado.
    """
    conn = get_connection()
    try:
        ids = _ids_descendientes(conn, carpeta_id)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM archivos_subidos WHERE carpeta_id IN %s",
                (tuple(ids),),
            )
            archivos = cur.fetchone()["n"]
    finally:
        conn.close()

    return {
        "subcarpetas": len(ids) - 1,
        "archivos": archivos,
    }


def eliminar_carpeta(carpeta_id: int) -> list[dict]:
    """
    Elimina una carpeta, todas sus subcarpetas y los registros de
    archivos_subidos contenidos en ellas (recursivamente).

    Devuelve los registros de archivos eliminados para que el router
    borre también los archivos físicos en Supabase Storage.
    """
    conn = get_connection()
    try:
        ids = _ids_descendientes(conn, carpeta_id)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM archivos_subidos WHERE carpeta_id IN %s",
                (tuple(ids),),
            )
            registros = [dict(f) for f in cur.fetchall()]

            cur.execute(
                "DELETE FROM archivos_subidos WHERE carpeta_id IN %s",
                (tuple(ids),),
            )
            cur.execute(
                "DELETE FROM carpetas WHERE id IN %s",
                (tuple(ids),),
            )

        conn.commit()
        return registros
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mover_archivo_a_carpeta(archivo_id: int, carpeta_id: int | None) -> None:
    """
    Mueve un archivo subido a otra carpeta (carpeta_id=None lo manda
    a la raíz).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE archivos_subidos SET carpeta_id = %s WHERE id = %s",
                (carpeta_id, archivo_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Archivos subidos
# ---------------------------------------------------------------------------

def guardar_archivo_subido(
    nombre_original: str,
    nombre_guardado: str,
    ruta: str,
    tamano_bytes: int,
    modulo: str | None = None,
    carpeta_id: int | None = None,
) -> int:
    """
    Registra un archivo subido manualmente.

    'ruta' ahora es el path dentro del bucket de Supabase Storage
    (por ejemplo "uploads/ab12cd34_reporte.pdf"), no una ruta de disco.

    Devuelve el ID asignado al archivo.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO archivos_subidos (
                    nombre_original, nombre_guardado, ruta,
                    tamano_bytes, modulo, carpeta_id, fecha_subida
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    nombre_original,
                    nombre_guardado,
                    ruta,
                    tamano_bytes,
                    modulo,
                    carpeta_id,
                    _ahora_bogota(),
                ),
            )
            nuevo_id = cur.fetchone()["id"]
        conn.commit()
        return nuevo_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def listar_archivos_subidos(carpeta_id: int | None = None) -> list[dict]:
    """
    Devuelve los archivos subidos dentro de una carpeta, ordenados del
    más reciente al más antiguo.

    carpeta_id=None devuelve los archivos que están en la raíz
    (sin carpeta asignada).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if carpeta_id is None:
                cur.execute(
                    """
                    SELECT * FROM archivos_subidos
                    WHERE carpeta_id IS NULL
                    ORDER BY id DESC
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM archivos_subidos
                    WHERE carpeta_id = %s
                    ORDER BY id DESC
                    """,
                    (carpeta_id,),
                )
            filas = cur.fetchall()
    finally:
        conn.close()

    return [dict(fila) for fila in filas]

    """
    Devuelve TODOS los archivos subidos etiquetados con un módulo
    específico (ej. "no_programados"), sin importar en qué carpeta
    estén ni cuándo se subieron.

    Pensado para módulos que ya no reciben archivos adjuntos en su
    propio formulario, sino que toman directamente lo que haya en la
    sección "Base de Datos" con ese módulo asignado.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM archivos_subidos WHERE modulo = %s ORDER BY id",
                (modulo,),
            )
            filas = cur.fetchall()
    finally:
        conn.close()

    return [dict(f) for f in filas]
def listar_archivos_por_modulo(modulo: str) -> list[dict]:
    """
    Devuelve TODOS los archivos subidos etiquetados con un módulo
    específico, sin importar mayúsculas, espacios o guiones.

    Así 'No Programados', 'no programados', 'NO_PROGRAMADOS' y
    'no_programados' se tratan como el mismo módulo.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM archivos_subidos
                WHERE LOWER(REPLACE(REPLACE(modulo, ' ', '_'), '-', '_')) = LOWER(%s)
                ORDER BY id
                """,
                (modulo,),
            )
            filas = cur.fetchall()
    finally:
        conn.close()

    return [dict(f) for f in filas]

def obtener_archivo_subido(archivo_id: int) -> dict | None:
    """
    Obtiene un archivo subido por su ID.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM archivos_subidos WHERE id = %s", (archivo_id,))
            fila = cur.fetchone()
    finally:
        conn.close()

    return dict(fila) if fila else None


def eliminar_archivo_subido(archivo_id: int) -> None:
    """
    Elimina del registro de la base de datos un archivo subido.

    Nota: esta función elimina el registro de Postgres, pero no elimina
    el archivo físico de Supabase Storage (eso lo hace el router).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM archivos_subidos WHERE id = %s", (archivo_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Borrado masivo (para el botón "Eliminar todo" de la página Base de Datos)
# ---------------------------------------------------------------------------

def vaciar_archivos_subidos() -> list[dict]:
    """
    Elimina TODOS los registros de archivos_subidos y TODAS las carpetas.
    Devuelve los registros de archivos eliminados para que el router pueda
    borrar también los archivos físicos de Supabase Storage.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM archivos_subidos")
            filas = [dict(f) for f in cur.fetchall()]

            cur.execute("DELETE FROM archivos_subidos")
            cur.execute("DELETE FROM carpetas")
        conn.commit()
        return filas
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def vaciar_archivos_generados() -> list[str]:
    """
    Vacía el JSON archivos_generados de TODAS las ejecuciones (dejando
    las filas de `ejecuciones` intactas para no perder el historial).
    Devuelve la lista de rutas (paths en Supabase Storage) que había
    registradas para que el router pueda borrarlas.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, archivos_generados FROM ejecuciones")
            filas = cur.fetchall()

            rutas: list[str] = []

            for fila in filas:
                archivos = fila["archivos_generados"] or {}
                rutas.extend(archivos.values())

                cur.execute(
                    "UPDATE ejecuciones SET archivos_generados = %s WHERE id = %s",
                    (psycopg2.extras.Json({}), fila["id"]),
                )

        conn.commit()
        return rutas
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()