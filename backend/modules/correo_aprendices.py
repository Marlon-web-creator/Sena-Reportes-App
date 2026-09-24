"""
modules/correo_aprendices.py

Cruza el número de documento del consolidado contra varios reportes de
aprendices .xls/.xlsx/.xlsm y escribe el correo encontrado en una columna
nueva del consolidado.

Tanto en el consolidado como en los reportes, la fila de inicio de los
datos y las columnas de documento/correo se detectan automáticamente,
igual que en no_aprobados.py:

  - La fila de encabezado se localiza puntuando cada celda contra un
    catálogo de encabezados típicos ("Documento", "Número de
    identificación", "Correo Electrónico", "Email", etc.). La fila con
    mayor puntaje acumulado es el encabezado; los datos empiezan justo
    debajo.
  - La columna del documento y la del correo se eligen por encabezado.
  - Si el correo no se localiza por encabezado, se busca por contenido
    (la columna con más valores con formato usuario@dominio).
  - Solo como último recurso se usa la posición histórica (B / F).

La columna de SALIDA en el consolidado tampoco es fija: se reutiliza la
columna "Correo Electrónico" si ya existe, o se crea justo después de la
última columna realmente usada de cada hoja.
"""

import re
import unicodedata
from pathlib import Path

from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from modules.file_utils import cargar_workbook_compatible

# Posiciones históricas, usadas solo si no se logra detectar la columna.
COL_DOCUMENTO_XLS_DEFECTO = 2            # B
COL_CORREO_XLS_DEFECTO = 6               # F
COL_DOCUMENTO_CONSOLIDADO_DEFECTO = 4    # D

ENCABEZADO_CORREO_SALIDA = "Correo Electrónico"

# Cuántas filas desde arriba se inspeccionan buscando el encabezado real
# (por si hay títulos, logos, filas vacías, etc.).
MAX_FILAS_BUSQUEDA_ENCABEZADO = 30

_REGEX_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_ENCABEZADOS_CORREO_EXACTOS = {
    "correo", "correo electronico", "email", "e mail", "mail",
    "correo institucional", "correo personal", "direccion de correo",
}
_ENCABEZADOS_DOCUMENTO_EXACTOS = {
    "documento", "numero documento", "numero de documento", "nro documento",
    "no documento", "n documento", "identificacion", "numero de identificacion",
    "nro identificacion", "cedula", "numero de cedula", "cc",
}


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

def _normalizar_documento(valor) -> str:
    """
    Normaliza números de documento para que coincidan aunque un archivo
    los tenga como texto y otro como número (ej. 123456 vs "123456.0").
    """
    if valor is None:
        return ""
    texto = str(valor).strip()
    if texto.endswith(".0"):
        texto = texto[:-2]
    return texto


def _normalizar_texto(valor) -> str:
    """Minúsculas, sin tildes ni signos, espacios colapsados (para comparar encabezados)."""
    if valor is None:
        return ""
    texto = unicodedata.normalize("NFKD", str(valor))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^a-z0-9 ]+", " ", texto.lower())
    return re.sub(r"\s+", " ", texto).strip()


# ---------------------------------------------------------------------------
# Detección de columnas / fila de inicio
# ---------------------------------------------------------------------------

def _puntuar_encabezado_correo(valor) -> int:
    texto = _normalizar_texto(valor)
    if not texto:
        return 0
    if texto in _ENCABEZADOS_CORREO_EXACTOS:
        return 2
    if "correo" in texto or "mail" in texto:
        return 1
    return 0


def _puntuar_encabezado_documento(valor) -> int:
    texto = _normalizar_texto(valor)
    if not texto or texto.startswith("tipo"):   # evita "Tipo de documento"
        return 0
    if texto in _ENCABEZADOS_DOCUMENTO_EXACTOS:
        return 2
    if any(clave in texto for clave in ("documento", "identificacion", "cedula")):
        return 1
    return 0


def _puntuar_encabezado_reporte(valor) -> int:
    """Puntúa una celda combinando encabezados de documento y correo."""
    return _puntuar_encabezado_documento(valor) + _puntuar_encabezado_correo(valor)


def _detectar_fila_inicio_datos(ws, puntuar, max_filas: int = MAX_FILAS_BUSQUEDA_ENCABEZADO) -> int:
    """
    Busca la fila con mayor puntaje acumulado (el encabezado real, aunque
    arriba haya títulos o filas vacías) y devuelve la fila donde empiezan
    los datos. Si no encuentra nada, cae al histórico (fila 2).
    """
    mejor_puntaje, mejor_fila = 0, 0
    limite = min(ws.max_row, max_filas)
    for fila in range(1, limite + 1):
        puntaje = sum(
            puntuar(ws.cell(fila, col).value)
            for col in range(1, ws.max_column + 1)
        )
        if puntaje > mejor_puntaje:
            mejor_puntaje, mejor_fila = puntaje, fila
    return (mejor_fila + 1) if mejor_fila else 2


def _buscar_columna_por_encabezado(ws, fila_inicio: int, puntuar) -> int | None:
    """
    Revisa las filas por encima de los datos, de abajo hacia arriba (la fila
    de encabezado real suele estar pegada a los datos; más arriba puede haber
    títulos). En la primera fila con coincidencias devuelve la mejor columna.
    """
    ultima_fila_encabezado = max(fila_inicio - 1, 1)
    for fila in range(ultima_fila_encabezado, 0, -1):
        mejor_puntaje, mejor_col = 0, None
        for col in range(1, ws.max_column + 1):
            puntaje = puntuar(ws.cell(fila, col).value)
            if puntaje > mejor_puntaje:
                mejor_puntaje, mejor_col = puntaje, col
        if mejor_col:
            return mejor_col
    return None


def _buscar_columna_correo_por_contenido(ws, fila_inicio: int, muestra: int = 300) -> int | None:
    """Devuelve la columna con más valores que parecen un correo, o None."""
    conteo: dict[int, int] = {}
    for fila in ws.iter_rows(
        min_row=fila_inicio,
        max_row=min(ws.max_row, fila_inicio + muestra - 1),
        values_only=True,
    ):
        for idx, valor in enumerate(fila, start=1):
            if isinstance(valor, str) and _REGEX_CORREO.match(valor.strip()):
                conteo[idx] = conteo.get(idx, 0) + 1
    return max(conteo, key=conteo.get) if conteo else None


def detectar_columnas_reporte(ws) -> dict:
    """
    Detecta fila de inicio, columna de documento y columna de correo de
    una hoja de reporte. No recibe filas ni columnas fijas: todo se
    infiere del contenido.

    Devuelve {"fila_inicio", "col_documento", "col_correo",
              "metodo_documento", "metodo_correo"}.
    """
    fila_inicio = _detectar_fila_inicio_datos(ws, _puntuar_encabezado_reporte)

    col_documento = _buscar_columna_por_encabezado(ws, fila_inicio, _puntuar_encabezado_documento)
    metodo_documento = "encabezado"
    if col_documento is None:
        col_documento = COL_DOCUMENTO_XLS_DEFECTO
        metodo_documento = "posicion_defecto"

    col_correo = _buscar_columna_por_encabezado(ws, fila_inicio, _puntuar_encabezado_correo)
    metodo_correo = "encabezado"
    if col_correo is None:
        col_correo = _buscar_columna_correo_por_contenido(ws, fila_inicio)
        metodo_correo = "contenido"
    if col_correo is None:
        col_correo = COL_CORREO_XLS_DEFECTO
        metodo_correo = "posicion_defecto"

    return {
        "fila_inicio": fila_inicio,
        "col_documento": col_documento,
        "col_correo": col_correo,
        "metodo_documento": metodo_documento,
        "metodo_correo": metodo_correo,
    }


# ---------------------------------------------------------------------------
# Mapa documento -> correo
# ---------------------------------------------------------------------------

def construir_mapa_documento_correo(
    archivos_xls: list[Path],
) -> tuple[dict, dict, list]:
    """
    Recorre todas las hojas de todos los reportes y arma:
    - mapa: documento normalizado -> correo (primer valor encontrado)
    - duplicados: documento -> lista de correos distintos encontrados
      (para avisar si el mismo documento aparece con más de un correo)
    - detalle_columnas: qué columnas se detectaron en cada archivo/hoja y
      con qué método (útil para auditar y para avisar de fallbacks)
    """
    mapa: dict[str, str] = {}
    duplicados: dict[str, list[str]] = {}
    detalle_columnas: list[dict] = []

    for archivo in archivos_xls:
        wb = cargar_workbook_compatible(archivo)
        for ws in wb.worksheets:
            cols = detectar_columnas_reporte(ws)
            col_doc, col_mail = cols["col_documento"], cols["col_correo"]
            fila_inicio = cols["fila_inicio"]

            detalle_columnas.append({
                "archivo": Path(archivo).name,
                "hoja": ws.title,
                "fila_inicio_datos": fila_inicio,
                "columna_documento": get_column_letter(col_doc),
                "columna_correo": get_column_letter(col_mail),
                "metodo_documento": cols["metodo_documento"],
                "metodo_correo": cols["metodo_correo"],
            })

            for fila in range(fila_inicio, ws.max_row + 1):
                documento = _normalizar_documento(ws.cell(fila, col_doc).value)
                correo_valor = ws.cell(fila, col_mail).value
                correo = str(correo_valor).strip() if correo_valor is not None else ""

                if not documento or not correo:
                    continue

                if documento not in mapa:
                    mapa[documento] = correo
                elif mapa[documento] != correo:
                    duplicados.setdefault(documento, [mapa[documento]])
                    if correo not in duplicados[documento]:
                        duplicados[documento].append(correo)

    return mapa, duplicados, detalle_columnas


# ---------------------------------------------------------------------------
# Detección en el consolidado
# ---------------------------------------------------------------------------

def _columna_salida(ws, fila_encabezado: int) -> int:
    """
    Decide en qué columna escribir los correos, sin posición fija:
    - si la hoja ya tiene un encabezado "Correo Electrónico", lo reutiliza;
    - si no, usa la columna siguiente a la última con contenido real
      (ws.max_column puede estar inflado por formatos vacíos).
    """
    ultima_usada = 0
    for fila in ws.iter_rows(values_only=True):
        for idx, valor in enumerate(fila, start=1):
            if valor not in (None, "") and idx > ultima_usada:
                ultima_usada = idx

    objetivo = _normalizar_texto(ENCABEZADO_CORREO_SALIDA)
    for col in range(1, ultima_usada + 1):
        if _normalizar_texto(ws.cell(fila_encabezado, col).value) == objetivo:
            return col

    return ultima_usada + 1


def _detectar_documento_consolidado(ws) -> tuple[int, int, int]:
    """
    Devuelve (fila_inicio, fila_encabezado, col_documento) detectados en
    una hoja del consolidado. Todo se infiere del contenido; si no se
    encuentra encabezado, se cae al histórico (fila 2, columna D).
    """
    fila_inicio = _detectar_fila_inicio_datos(ws, _puntuar_encabezado_reporte)
    fila_encabezado = max(fila_inicio - 1, 1)

    col_documento = _buscar_columna_por_encabezado(
        ws, fila_inicio, _puntuar_encabezado_documento
    )
    if col_documento is None:
        col_documento = COL_DOCUMENTO_CONSOLIDADO_DEFECTO

    return fila_inicio, fila_encabezado, col_documento


# ---------------------------------------------------------------------------
# Proceso principal
# ---------------------------------------------------------------------------

def enriquecer_con_correos(
    consolidado: Path,
    archivos_xls: list[Path],
    salida_dir: Path,
) -> dict:
    """
    Devuelve estadísticas + ruta del archivo generado. El consolidado
    original no se modifica: se guarda una copia procesada en salida_dir.

    Ni la fila de inicio ni las columnas se reciben por parámetro: se
    detectan por encabezado/contenido en cada hoja.
    """
    salida_dir.mkdir(parents=True, exist_ok=True)

    mapa_correos, duplicados, detalle_columnas = construir_mapa_documento_correo(
        archivos_xls
    )

    advertencias = [
        f"{d['archivo']} / {d['hoja']}: columna de correo tomada por "
        f"{d['metodo_correo']} ({d['columna_correo']}), documento por "
        f"{d['metodo_documento']} ({d['columna_documento']})."
        for d in detalle_columnas
        if "posicion_defecto" in (d["metodo_correo"], d["metodo_documento"])
        or d["metodo_correo"] == "contenido"
    ]

    wb = cargar_workbook_compatible(consolidado)

    total_documentos = 0
    total_encontrados = 0
    documentos_sin_correo = []
    columnas_salida = []

    for ws in wb.worksheets:
        fila_inicio, fila_encabezado, col_documento = _detectar_documento_consolidado(ws)

        col_salida = _columna_salida(ws, fila_encabezado)
        columnas_salida.append({
            "hoja": ws.title,
            "fila_inicio_datos": fila_inicio,
            "columna_documento": get_column_letter(col_documento),
            "columna_salida": get_column_letter(col_salida),
        })
        ws.cell(fila_encabezado, col_salida, ENCABEZADO_CORREO_SALIDA).font = Font(bold=True)

        for fila in range(fila_inicio, ws.max_row + 1):
            documento_valor = ws.cell(fila, col_documento).value
            documento = _normalizar_documento(documento_valor)
            if not documento:
                continue

            total_documentos += 1
            correo = mapa_correos.get(documento)

            if correo:
                ws.cell(fila, col_salida, correo)
                total_encontrados += 1
            else:
                documentos_sin_correo.append(documento)

    nombre_salida = Path(consolidado).stem + "_ConCorreos.xlsx"
    ruta_salida = salida_dir / nombre_salida
    wb.save(ruta_salida)

    return {
        "archivo_generado": str(ruta_salida),
        "total_correos_indexados": len(mapa_correos),
        "total_documentos_consolidado": total_documentos,
        "total_encontrados": total_encontrados,
        "total_sin_correo": len(documentos_sin_correo),
        "documentos_sin_correo": documentos_sin_correo[:50],
        "documentos_con_correos_distintos": duplicados,
        "columnas_detectadas_reportes": detalle_columnas,
        "columnas_salida_consolidado": columnas_salida,
        "advertencias": advertencias,
    }