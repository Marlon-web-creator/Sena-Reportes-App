"""
database.py

Conexión SQLite para guardar:
- Historial de ejecuciones de los módulos.
- Archivos subidos manualmente desde la sección "Base de Datos".
- Carpetas y subcarpetas para organizar esos archivos.

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
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Migraciones ligeras (para bases de datos ya existentes)
# ---------------------------------------------------------------------------

def _migrar_columnas_faltantes(conn: sqlite3.Connection) -> None:
    """
    Agrega columnas nuevas a tablas que ya existían antes de introducir
    carpetas, sin romper instalaciones previas.
    """

    columnas = {
        fila["name"]
        for fila in conn.execute("PRAGMA table_info(archivos_subidos)").fetchall()
    }

    if "carpeta_id" not in columnas:
        conn.execute(
            "ALTER TABLE archivos_subidos ADD COLUMN carpeta_id INTEGER "
            "REFERENCES carpetas(id)"
        )


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
    # Carpetas (deben crearse antes que archivos_subidos por la FK)
    # -----------------------------------------------------------------------

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS carpetas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            padre_id INTEGER,
            fecha_creacion TEXT NOT NULL,
            FOREIGN KEY (padre_id) REFERENCES carpetas (id)
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
            carpeta_id INTEGER,
            fecha_subida TEXT NOT NULL,
            FOREIGN KEY (carpeta_id) REFERENCES carpetas (id)
        )
        """
    )

    _migrar_columnas_faltantes(conn)

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
# Carpetas
# ---------------------------------------------------------------------------

def crear_carpeta(nombre: str, padre_id: int | None = None) -> int:
    """
    Crea una carpeta (opcionalmente dentro de otra) y devuelve su ID.
    """

    conn = get_connection()

    cur = conn.execute(
        """
        INSERT INTO carpetas (nombre, padre_id, fecha_creacion)
        VALUES (?, ?, ?)
        """,
        (nombre.strip(), padre_id, _ahora_bogota()),
    )

    conn.commit()

    nuevo_id = cur.lastrowid

    conn.close()

    return nuevo_id


def obtener_carpeta(carpeta_id: int) -> dict | None:
    """
    Obtiene una carpeta por su ID.
    """

    conn = get_connection()

    fila = conn.execute(
        "SELECT * FROM carpetas WHERE id = ?",
        (carpeta_id,),
    ).fetchone()

    conn.close()

    return dict(fila) if fila else None


def listar_carpetas(padre_id: int | None = None) -> list[dict]:
    """
    Lista las subcarpetas directas de padre_id.
    padre_id=None devuelve las carpetas de la raíz.
    """

    conn = get_connection()

    if padre_id is None:
        filas = conn.execute(
            """
            SELECT * FROM carpetas
            WHERE padre_id IS NULL
            ORDER BY nombre COLLATE NOCASE
            """
        ).fetchall()
    else:
        filas = conn.execute(
            """
            SELECT * FROM carpetas
            WHERE padre_id = ?
            ORDER BY nombre COLLATE NOCASE
            """,
            (padre_id,),
        ).fetchall()

    conn.close()

    return [dict(f) for f in filas]


def listar_todas_las_carpetas() -> list[dict]:
    """
    Devuelve todas las carpetas (planas, con su padre_id), útil para
    construir un árbol completo o un selector de "mover a...".
    """

    conn = get_connection()

    filas = conn.execute(
        "SELECT * FROM carpetas ORDER BY nombre COLLATE NOCASE"
    ).fetchall()

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

    while actual_id is not None:
        if actual_id in visitados:
            break  # protección ante ciclos accidentales

        visitados.add(actual_id)

        fila = conn.execute(
            "SELECT id, nombre, padre_id FROM carpetas WHERE id = ?",
            (actual_id,),
        ).fetchone()

        if not fila:
            break

        ruta.insert(0, {"id": fila["id"], "nombre": fila["nombre"]})

        actual_id = fila["padre_id"]

    conn.close()

    return ruta


def renombrar_carpeta(carpeta_id: int, nuevo_nombre: str) -> None:
    """
    Cambia el nombre de una carpeta.
    """

    conn = get_connection()

    conn.execute(
        "UPDATE carpetas SET nombre = ? WHERE id = ?",
        (nuevo_nombre.strip(), carpeta_id),
    )

    conn.commit()
    conn.close()


def _ids_descendientes(conn: sqlite3.Connection, carpeta_id: int) -> list[int]:
    """
    BFS: devuelve carpeta_id y los IDs de todas sus subcarpetas,
    recursivamente.
    """

    ids = [carpeta_id]
    pendientes = [carpeta_id]

    while pendientes:
        actual = pendientes.pop()

        hijos = conn.execute(
            "SELECT id FROM carpetas WHERE padre_id = ?",
            (actual,),
        ).fetchall()

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

    ids = _ids_descendientes(conn, carpeta_id)
    placeholders = ",".join("?" * len(ids))

    archivos = conn.execute(
        f"SELECT COUNT(*) AS n FROM archivos_subidos WHERE carpeta_id IN ({placeholders})",
        ids,
    ).fetchone()["n"]

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
    borre también los archivos físicos del disco.
    """

    conn = get_connection()

    ids = _ids_descendientes(conn, carpeta_id)
    placeholders = ",".join("?" * len(ids))

    filas_archivos = conn.execute(
        f"SELECT * FROM archivos_subidos WHERE carpeta_id IN ({placeholders})",
        ids,
    ).fetchall()

    registros = [dict(f) for f in filas_archivos]

    conn.execute(
        f"DELETE FROM archivos_subidos WHERE carpeta_id IN ({placeholders})",
        ids,
    )

    conn.execute(
        f"DELETE FROM carpetas WHERE id IN ({placeholders})",
        ids,
    )

    conn.commit()
    conn.close()

    return registros


def mover_archivo_a_carpeta(archivo_id: int, carpeta_id: int | None) -> None:
    """
    Mueve un archivo subido a otra carpeta (carpeta_id=None lo manda
    a la raíz).
    """

    conn = get_connection()

    conn.execute(
        "UPDATE archivos_subidos SET carpeta_id = ? WHERE id = ?",
        (carpeta_id, archivo_id),
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
    carpeta_id: int | None = None,
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
            carpeta_id,
            fecha_subida
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
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

    conn.commit()

    nuevo_id = cur.lastrowid

    conn.close()

    return nuevo_id


def listar_archivos_subidos(carpeta_id: int | None = None) -> list[dict]:
    """
    Devuelve los archivos subidos dentro de una carpeta, ordenados del
    más reciente al más antiguo.

    carpeta_id=None devuelve los archivos que están en la raíz
    (sin carpeta asignada).
    """

    conn = get_connection()

    if carpeta_id is None:
        filas = conn.execute(
            """
            SELECT *
            FROM archivos_subidos
            WHERE carpeta_id IS NULL
            ORDER BY id DESC
            """
        ).fetchall()
    else:
        filas = conn.execute(
            """
            SELECT *
            FROM archivos_subidos
            WHERE carpeta_id = ?
            ORDER BY id DESC
            """,
            (carpeta_id,),
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
    Elimina TODOS los registros de archivos_subidos y TODAS las carpetas.
    Devuelve los registros de archivos eliminados para que el router pueda
    borrar también los archivos físicos del disco.
    """
    conn = get_connection()

    filas = conn.execute("SELECT * FROM archivos_subidos").fetchall()
    conn.execute("DELETE FROM archivos_subidos")
    conn.execute("DELETE FROM carpetas")
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