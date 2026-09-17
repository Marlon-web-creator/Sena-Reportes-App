"""
database.py

Conexión SQLite para guardar:
- Historial de ejecuciones de los módulos.
- Archivos subidos manualmente desde la sección "Base de Datos".

La base de datos se guarda como app.db en la raíz del proyecto.

Notas:
- Todas las fechas se guardan en hora de Colombia (America/Bogota, UTC-5),
  formateadas como d/m/aaaa hh:mm:ss (ej. 2/12/2026 14:30:05).
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).resolve().parent.parent / "app.db"

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

def get_connection() -> sqlite3.Connection:
    """
    Crea y devuelve una conexión a la base de datos SQLite.
    """

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Inicialización de la base de datos
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Crea las tablas necesarias si todavía no existen.
    """

    conn = get_connection()

    # -----------------------------------------------------------------------
    # Historial de ejecuciones
    # -----------------------------------------------------------------------

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ejecuciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            modulo TEXT NOT NULL,
            fecha TEXT NOT NULL,
            parametros TEXT,
            resultado TEXT,
            archivos_generados TEXT
        )
        """
    )

    # -----------------------------------------------------------------------
    # Archivos subidos manualmente
    # -----------------------------------------------------------------------

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS archivos_subidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_original TEXT NOT NULL,
            nombre_guardado TEXT NOT NULL,
            ruta TEXT NOT NULL,
            tamano_bytes INTEGER,
            modulo TEXT,
            fecha_subida TEXT NOT NULL
        )
        """
    )

    conn.commit()
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

    cur = conn.execute(
        """
        INSERT INTO ejecuciones (
            modulo,
            fecha,
            parametros,
            resultado,
            archivos_generados
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            modulo,
            _ahora_bogota(),
            json.dumps(parametros, ensure_ascii=False),
            json.dumps(resultado, ensure_ascii=False),
            json.dumps(archivos_generados, ensure_ascii=False),
        ),
    )

    conn.commit()

    nuevo_id = cur.lastrowid

    conn.close()

    return nuevo_id


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

    if modulo:
        filas = conn.execute(
            """
            SELECT *
            FROM ejecuciones
            WHERE modulo = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (modulo, limite),
        ).fetchall()
    else:
        filas = conn.execute(
            """
            SELECT *
            FROM ejecuciones
            ORDER BY id DESC
            LIMIT ?
            """,
            (limite,),
        ).fetchall()

    conn.close()

    resultado = []

    for fila in filas:
        item = dict(fila)

        item["parametros"] = json.loads(
            item["parametros"] or "{}"
        )

        item["resultado"] = json.loads(
            item["resultado"] or "{}"
        )

        item["archivos_generados"] = json.loads(
            item["archivos_generados"] or "{}"
        )

        resultado.append(item)

    return resultado


def obtener_ejecucion(
    ejecucion_id: int,
) -> dict | None:
    """
    Obtiene una ejecución específica por su ID.
    """

    conn = get_connection()

    fila = conn.execute(
        """
        SELECT *
        FROM ejecuciones
        WHERE id = ?
        """,
        (ejecucion_id,),
    ).fetchone()

    conn.close()

    if not fila:
        return None

    item = dict(fila)

    item["parametros"] = json.loads(
        item["parametros"] or "{}"
    )

    item["resultado"] = json.loads(
        item["resultado"] or "{}"
    )

    item["archivos_generados"] = json.loads(
        item["archivos_generados"] or "{}"
    )

    return item


def quitar_archivo_generado(
    ejecucion_id: int,
    clave: str,
) -> None:
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

    conn.execute(
        """
        UPDATE ejecuciones
        SET archivos_generados = ?
        WHERE id = ?
        """,
        (
            json.dumps(
                archivos,
                ensure_ascii=False,
            ),
            ejecucion_id,
        ),
    )

    conn.commit()
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
) -> int:
    """
    Registra un archivo subido manualmente.

    Devuelve el ID asignado al archivo.
    """

    conn = get_connection()

    cur = conn.execute(
        """
        INSERT INTO archivos_subidos (
            nombre_original,
            nombre_guardado,
            ruta,
            tamano_bytes,
            modulo,
            fecha_subida
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            nombre_original,
            nombre_guardado,
            ruta,
            tamano_bytes,
            modulo,
            _ahora_bogota(),
        ),
    )

    conn.commit()

    nuevo_id = cur.lastrowid

    conn.close()

    return nuevo_id


def listar_archivos_subidos() -> list[dict]:
    """
    Devuelve todos los archivos subidos, ordenados del más reciente
    al más antiguo.
    """

    conn = get_connection()

    filas = conn.execute(
        """
        SELECT *
        FROM archivos_subidos
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return [dict(fila) for fila in filas]


def obtener_archivo_subido(
    archivo_id: int,
) -> dict | None:
    """
    Obtiene un archivo subido por su ID.
    """

    conn = get_connection()

    fila = conn.execute(
        """
        SELECT *
        FROM archivos_subidos
        WHERE id = ?
        """,
        (archivo_id,),
    ).fetchone()

    conn.close()

    if not fila:
        return None

    return dict(fila)


def eliminar_archivo_subido(
    archivo_id: int,
) -> None:
    """
    Elimina del registro de la base de datos un archivo subido.

    Nota:
    Esta función elimina el registro de SQLite, pero no elimina
    físicamente el archivo del disco.
    """

    conn = get_connection()

    conn.execute(
        """
        DELETE FROM archivos_subidos
        WHERE id = ?
        """,
        (archivo_id,),
    )

    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Borrado masivo (para el botón "Eliminar todo" de la página Base de Datos)
# ---------------------------------------------------------------------------

def vaciar_archivos_subidos() -> list[dict]:
    """
    Elimina TODOS los registros de archivos_subidos.
    Devuelve los registros eliminados para que el router pueda borrar
    también los archivos físicos del disco.
    """
    conn = get_connection()

    filas = conn.execute("SELECT * FROM archivos_subidos").fetchall()
    conn.execute("DELETE FROM archivos_subidos")
    conn.commit()
    conn.close()

    return [dict(f) for f in filas]


def vaciar_archivos_generados() -> list[str]:
    """
    Vacía el JSON archivos_generados de TODAS las ejecuciones (dejando
    las filas de `ejecuciones` intactas para no perder el historial).
    Devuelve la lista de rutas que había registradas para que el router
    pueda borrarlas del disco.
    """
    conn = get_connection()

    filas = conn.execute(
        "SELECT id, archivos_generados FROM ejecuciones"
    ).fetchall()

    rutas: list[str] = []

    for fila in filas:
        try:
            archivos = json.loads(fila["archivos_generados"] or "{}")
        except Exception:
            archivos = {}

        rutas.extend(archivos.values())

        conn.execute(
            "UPDATE ejecuciones SET archivos_generados = ? WHERE id = ?",
            ("{}", fila["id"]),
        )

    conn.commit()
    conn.close()

    return rutas