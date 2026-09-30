"""
modules/no_programados.py

Verificador No Programados — versión alimentada por EXCELS de la Base de Datos.

Antes: para cada competencia del Consolidado_General se buscaba su texto
dentro de PDFs de programación (texto digital + OCR).

Ahora: la programación sale de Excels exportados (hoja "Export", una fila
por aprendiz x RAP) que viven en la sección "Base de Datos". Para cada fila
del Consolidado se ubican, en esos Excels, los RAP de esa competencia para
ese aprendiz en esa ficha, y cada RAP se clasifica en uno de 4 casos:

    RAP EVALUADO CON PROGRAMACIÓN
    RAP EVALUADO SIN PROGRAMACIÓN
    RAP SIN EVALUAR CON PROGRAMACIÓN
    RAP SIN EVALUAR SIN PROGRAMACIÓN

Criterio (se deriva de las columnas del export, no de texto libre):
    - EVALUADO      -> hay instructor que emitió el juicio
                       (DOCUMENTO INSTRUCTOR / NOMBRE INSTRUCTOR EMITIÓ JUICIO)
    - CON PROGRAMACIÓN -> hay instructor programado
                       (DOCUMENTO INSTRUCTOR PROGRAMADO / NOMBRE INSTRUCTOR PROGRAMADO)

Llave de cruce Consolidado <-> export:
    ficha (título de la hoja) + documento del aprendiz + competencia.

Ya no se usa PyMuPDF, pytesseract ni OCR.
"""

import logging
import math
import os
import re
import time
import unicodedata
from collections import Counter
from copy import copy
from pathlib import Path
from typing import Callable, Optional

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.table import TableColumn
from rapidfuzz import fuzz

# ============================================================
# LOGGING
# ============================================================
logger = logging.getLogger("no_programados")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] no_programados: %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(_h)
    logger.propagate = False

# ============================================================
# CONFIGURACIÓN — CONSOLIDADO (entrada / salida)
# ============================================================

FILA_INICIO_DATOS = 5

# El encabezado se busca dentro de este rango de filas (no se asume la 4).
FILA_ENCABEZADO_MIN = 1
FILA_ENCABEZADO_MAX = 8

CLAVES_COL_CODIGO = ["CODIGO"]
CLAVES_COL_COMPETENCIA = ["COMPETENCIA"]

# Documento por NIVELES de prioridad: "Número de Documento" le gana a
# "Tipo de Documento" (ambos contienen la palabra DOCUMENTO).
NIVELES_CLAVES_COL_DOCUMENTO = [
    ["NUMERO DE DOCUMENTO", "NUMERO DOCUMENTO", "N DOCUMENTO", "NO DOCUMENTO"],
    ["IDENTIFICACION", "CEDULA"],
    ["DOCUMENTO"],
]

# Columnas de SALIDA: se reutilizan si ya existen (mismo encabezado) o se
# crean después de la última columna con datos. Nunca se pisa una columna
# que ya tenía información.
ENCABEZADO_ESTADO = "Estado Programación"
ENCABEZADO_OBSERVACION = "Observación"
ENCABEZADO_ARCHIVOS = "Archivo(s) BD"
ENCABEZADO_DETALLE = "Detalle por RAP"

# Las columnas de salida se definen UNA sola vez aquí: (clave, encabezado,
# ancho en caracteres). Para agregar o quitar una columna basta tocar esta
# lista; su posición no está fija: se ubican a continuación de la última
# columna con encabezado de la tabla del Consolidado (detectada en cada
# hoja) y heredan el estilo de la tabla (ver `aplicar_formato_salida`).
COLUMNAS_SALIDA = (
    ("estado", ENCABEZADO_ESTADO, 34),
    ("observacion", ENCABEZADO_OBSERVACION, 40),
    ("archivos", ENCABEZADO_ARCHIVOS, 28),
    ("detalle", ENCABEZADO_DETALLE, 60),
)
_ENCABEZADOS_SALIDA_NORM = None  # se calcula tras definir normalizar_texto

# Altura de fila: el texto extenso de las columnas nuevas se ajusta (wrap)
# pero la fila NUNCA crece más de este número de líneas por culpa de ellas.
# El texto completo sigue en la celda (se ve al seleccionarla) y por RAP en
# la hoja "Reporte". Ajustable con NP_MAX_LINEAS_FILA.
MAX_LINEAS_FILA = int(os.environ.get("NP_MAX_LINEAS_FILA") or 3)
ALTO_LINEA_PT = 15.0
ANCHO_DEFECTO_COL = 13

# ============================================================
# CONFIGURACIÓN — EXCELS DE LA BASE DE DATOS (export)
# ============================================================

FILA_ENCABEZADO_EXPORT_MAX = int(os.environ.get("NP_FILA_ENCABEZADO_EXPORT_MAX") or 15)

# Encabezados del export, YA normalizados (sin tildes, sin signos, en
# mayúsculas). Se buscan por coincidencia EXACTA para que, por ejemplo,
# "DOCUMENTO INSTRUCTOR" no se confunda con "DOCUMENTO INSTRUCTOR PROGRAMADO".
EXP_GRUPO = "GRUPO"
EXP_DOC_APRENDIZ = "NUMERO DOCUMENTO APRENDIZ"
EXP_COMPETENCIA = "COMPETENCIA"
EXP_ID_COMPETENCIA = "ID COMPETENCIA"
EXP_ID_RAP = "ID RAP"
EXP_RAP = "RAP"
EXP_DOC_JUICIO = "DOCUMENTO INSTRUCTOR"
EXP_NOM_JUICIO = "NOMBRE INSTRUCTOR EMITIO JUICIO"
EXP_DOC_PROG = "DOCUMENTO INSTRUCTOR PROGRAMADO"
EXP_NOM_PROG = "NOMBRE INSTRUCTOR PROGRAMADO"
EXP_ETIQUETA_REPORTE = "COINCIDE INSTRUCTOR"   # opcional: etiqueta que ya trae el export

# Un instructor "no cuenta" si la celda está vacía o trae un marcador de
# relleno. Ajusta estos sets si tus exports usan otros textos.
PLACEHOLDERS_INSTRUCTOR_EXACTOS = {
    "N A", "NA", "NULL", "NONE", "SIN ASIGNAR", "POR ASIGNAR", "SIN INSTRUCTOR",
}
PLACEHOLDERS_INSTRUCTOR_PALABRAS = {"PENDIENTE"}

# Similitud mínima (0-1) para aceptar un nombre de competencia distinto
# pero casi igual entre Consolidado y export (cuando no coincide exacto).
UMBRAL_COMPETENCIA = float(os.environ.get("NP_UMBRAL_COMPETENCIA") or 0.90)

MIN_DIGITOS_FICHA = 5

# ============================================================
# CASOS
# ============================================================

CASOS = (
    "RAP EVALUADO CON PROGRAMACIÓN",
    "RAP EVALUADO SIN PROGRAMACIÓN",
    "RAP SIN EVALUAR CON PROGRAMACIÓN",
    "RAP SIN EVALUAR SIN PROGRAMACIÓN",
)
# Orden con el que se reordenan las filas de cada aprendiz (el orden en que
# se listaron los casos). Las filas sin coincidencia en BD quedan al final.
ORDEN_CASO = {caso: i for i, caso in enumerate(CASOS)}
ETIQUETA_SIN_COINCIDENCIA = "SIN COINCIDENCIA EN BD"
ORDEN_SIN_COINCIDENCIA = len(CASOS)

MOTIVOS_SIN_COINCIDENCIA = {
    "SIN_DOCUMENTO": "La fila no tiene documento de aprendiz.",
    "SIN_FICHA": "La ficha no aparece en ningún Excel de la Base de Datos.",
    "SIN_APRENDIZ": "El aprendiz no aparece en esa ficha en la Base de Datos.",
    "SIN_COMPETENCIA": "La competencia no aparece para este aprendiz en la Base de Datos.",
}

ProgressCallback = Optional[Callable[[str, int, int], None]]


def _notificar(callback: ProgressCallback, mensaje: str, actual: int = 0, total: int = 0):
    logger.info("[progreso] %s (%d/%d)", mensaje, actual, total)
    if callback:
        try:
            callback(mensaje, actual, total)
        except Exception:
            logger.exception("callback de progreso falló")


# ============================================================
# TEXTO
# ============================================================

def normalizar_texto(texto):
    if not texto:
        return ""
    texto = str(texto).upper()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    for viejo, nuevo in {"\n": " ", "\r": " ", "\t": " ", "–": "-", "—": "-",
                          "_": " ", "/": " ", "\\": " "}.items():
        texto = texto.replace(viejo, nuevo)
    texto = re.sub(r"[^A-Z0-9 ]+", " ", texto)
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def _texto_celda(valor) -> str:
    """Celda -> texto limpio. Un float entero (1000930633.0) pierde el '.0'."""
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return str(valor).strip()


def _digitos(valor) -> str:
    return re.sub(r"\D", "", _texto_celda(valor))


def _ficha_de_titulo(titulo: str) -> str:
    """Primer número de >= MIN_DIGITOS_FICHA dígitos del título de la hoja."""
    m = re.search(rf"(?<!\d)(\d{{{MIN_DIGITOS_FICHA},}})(?!\d)", str(titulo))
    return m.group(1) if m else ""


def _instructor_valido(nombre, documento) -> bool:
    """True si al menos uno de los dos campos trae un instructor real."""
    for valor in (nombre, documento):
        texto = normalizar_texto(_texto_celda(valor))
        if not texto:
            continue
        if texto in PLACEHOLDERS_INSTRUCTOR_EXACTOS:
            continue
        if any(p in PLACEHOLDERS_INSTRUCTOR_PALABRAS for p in texto.split()):
            continue
        return True
    return False


def _etiqueta_instructor(nombre, documento) -> str:
    n, d = _texto_celda(nombre), _texto_celda(documento)
    if n and d:
        return f"{n} ({d})"
    return n or d


def clasificar_rap(evaluado: bool, programado: bool) -> str:
    return (
        f"RAP {'EVALUADO' if evaluado else 'SIN EVALUAR'} "
        f"{'CON' if programado else 'SIN'} PROGRAMACIÓN"
    )


_CASOS_NORMALIZADOS = {normalizar_texto(c): c for c in CASOS}
_ENCABEZADOS_SALIDA_NORM = {normalizar_texto(enc) for _, enc, _ in COLUMNAS_SALIDA}


# ============================================================
# DETECCIÓN DE COLUMNAS DEL CONSOLIDADO (por encabezado)
# ============================================================

class ColumnasNoDetectadas(Exception):
    """
    Hoja sin estructura de encabezado reconocible. No debe tumbar el
    procesamiento de las demás hojas: `procesar()` la captura, registra la
    hoja como omitida y sigue.
    """
    pass


class ColumnasHoja:
    """Mapa de columnas resuelto para una hoja (ficha) del Consolidado."""
    __slots__ = (
        "fila_encabezado",
        "codigo", "competencia", "documento",
        "estado", "observacion", "archivos", "detalle",
        "ultima_col_tabla", "col_estilo", "salida", "creadas",
    )

    def __repr__(self):
        return (
            f"ColumnasHoja(fila_encabezado={self.fila_encabezado}, "
            f"codigo={self.codigo}, competencia={self.competencia}, "
            f"documento={self.documento}, estado={self.estado}, "
            f"observacion={self.observacion}, archivos={self.archivos}, "
            f"detalle={self.detalle})"
        )


def _buscar_columna_por_claves(ws, fila_encabezado, claves, max_col):
    for col in range(1, max_col + 1):
        valor = ws.cell(fila_encabezado, col).value
        if valor is None:
            continue
        texto = normalizar_texto(valor)
        if any(clave in texto for clave in claves):
            return col
    return None


def _buscar_columna_por_niveles(ws, fila_encabezado, niveles, max_col):
    for claves in niveles:
        col = _buscar_columna_por_claves(ws, fila_encabezado, claves, max_col)
        if col is not None:
            return col
    return None


def _buscar_columna_por_texto_exacto(ws, fila_encabezado, texto_buscado, max_col):
    objetivo = normalizar_texto(texto_buscado)
    for col in range(1, max_col + 1):
        valor = ws.cell(fila_encabezado, col).value
        if valor is not None and normalizar_texto(valor) == objetivo:
            return col
    return None


def _ultima_col_con_encabezado(ws, fila_encabezado, max_col, excluir=frozenset()):
    """Última columna cuyo encabezado tiene texto (ignora las columnas de salida)."""
    ultima = 0
    for col in range(1, max_col + 1):
        valor = ws.cell(fila_encabezado, col).value
        if valor is None or not str(valor).strip():
            continue
        if normalizar_texto(valor) in excluir:
            continue
        ultima = col
    return ultima


def _columna_vacia(ws, col, fila_desde):
    """True si la columna no tiene ningún valor desde `fila_desde` hacia abajo."""
    for (valor,) in ws.iter_rows(
        min_row=fila_desde, max_row=ws.max_row, min_col=col, max_col=col, values_only=True
    ):
        if valor is not None and str(valor).strip() != "":
            return False
    return True


def _localizar_fila_encabezado(ws, max_col):
    limite = min(max_col, ws.max_column) if max_col else ws.max_column
    for fila in range(FILA_ENCABEZADO_MIN, FILA_ENCABEZADO_MAX + 1):
        tiene_codigo = _buscar_columna_por_claves(ws, fila, CLAVES_COL_CODIGO, limite) is not None
        tiene_competencia = _buscar_columna_por_claves(ws, fila, CLAVES_COL_COMPETENCIA, limite) is not None
        if tiene_codigo and tiene_competencia:
            return fila
    return None


def detectar_columnas(ws) -> ColumnasHoja:
    """
    Ubica las columnas del Consolidado leyendo el texto real de su fila de
    encabezado. Entrada (código, competencia, documento): deben existir.
    Salida (estado, observación, archivos, detalle): se reutilizan si ya
    existen o se crean después de la última columna con datos.
    """
    max_col_original = ws.max_column

    fila_encabezado = _localizar_fila_encabezado(ws, max_col_original)
    if fila_encabezado is None:
        raise ColumnasNoDetectadas(
            f"Hoja '{ws.title}': no se encontró una fila con encabezados "
            f"'Código' y 'Competencia' entre las filas "
            f"{FILA_ENCABEZADO_MIN} y {FILA_ENCABEZADO_MAX}. Esta hoja se "
            "omitirá; revisa su estructura (o amplía FILA_ENCABEZADO_MAX "
            "si el título ocupa más filas de lo normal)."
        )

    cols = ColumnasHoja()
    cols.fila_encabezado = fila_encabezado

    cols.codigo = _buscar_columna_por_claves(
        ws, fila_encabezado, CLAVES_COL_CODIGO, max_col_original
    )
    cols.competencia = _buscar_columna_por_claves(
        ws, fila_encabezado, CLAVES_COL_COMPETENCIA, max_col_original
    )
    cols.documento = _buscar_columna_por_niveles(
        ws, fila_encabezado, NIVELES_CLAVES_COL_DOCUMENTO, max_col_original
    )
    if cols.documento is None:
        raise ColumnasNoDetectadas(
            f"Hoja '{ws.title}': se encontró el encabezado en la fila "
            f"{fila_encabezado}, pero no la columna de 'Documento' "
            "(identificación del aprendiz, usada para cruzar con la Base de "
            "Datos). Ajusta NIVELES_CLAVES_COL_DOCUMENTO con el texto real "
            "de esa columna."
        )

    # Columnas de salida "procedurales": se ubican después de la última
    # columna con encabezado de la tabla (no de ws.max_column, que puede
    # incluir columnas vacías con formato), se reutilizan si ya existen
    # (mismo encabezado) y nunca pisan una columna que tenga datos.
    cols.ultima_col_tabla = _ultima_col_con_encabezado(
        ws, fila_encabezado, max_col_original, excluir=_ENCABEZADOS_SALIDA_NORM
    )
    cols.col_estilo = cols.competencia   # columna de texto que se toma como modelo de estilo
    cols.salida = {}
    cols.creadas = set()

    usadas = set()
    siguiente = cols.ultima_col_tabla + 1
    for clave, encabezado, _ancho in COLUMNAS_SALIDA:
        col = _buscar_columna_por_texto_exacto(ws, fila_encabezado, encabezado, max_col_original)
        if col is None:
            while siguiente in usadas or not _columna_vacia(ws, siguiente, fila_encabezado):
                siguiente += 1
            col = siguiente
            siguiente += 1
            cols.creadas.add(clave)
        usadas.add(col)
        cols.salida[clave] = col

    cols.estado = cols.salida["estado"]
    cols.observacion = cols.salida["observacion"]
    cols.archivos = cols.salida["archivos"]
    cols.detalle = cols.salida["detalle"]

    return cols


def extraer_competencia(valor):
    if valor is None:
        return None, None
    original = str(valor).strip()
    if not original:
        return None, None

    patron = re.match(r"^\s*(\d+)\s*[-–—:]\s*(.*)$", original)
    if patron:
        codigo, descripcion = patron.group(1), patron.group(2)
    else:
        codigo_match = re.match(r"^\s*(\d+)\s+", original)
        if codigo_match:
            codigo = codigo_match.group(1)
            descripcion = original[codigo_match.end():]
        else:
            codigo, descripcion = None, original

    descripcion = normalizar_texto(descripcion)
    return (codigo, descripcion) if descripcion else (codigo, None)


def fila_es_valida(ws, fila, cols: "ColumnasHoja"):
    valor_codigo = ws.cell(fila, cols.codigo).value
    valor_competencia = ws.cell(fila, cols.competencia).value
    if valor_codigo is None or valor_competencia is None:
        return False
    texto_codigo = normalizar_texto(valor_codigo)
    if not texto_codigo or "TOTAL" in texto_codigo or texto_codigo == "CODIGO":
        return False
    if fila <= cols.fila_encabezado:
        return False
    return bool(str(valor_competencia).strip())


# ============================================================
# EXCELS DE LA BASE DE DATOS
# ============================================================

class ExportNoReconocido(Exception):
    """Una hoja de un Excel de BD no tiene la estructura esperada del export."""
    pass


class BaseProgramacion:
    """
    Índice en memoria de todos los Excels de la BD.

        indice[(ficha, documento_aprendiz)][competencia_normalizada] = {
            "competencia": texto original,
            "id_competencia": str,
            "raps": {llave_rap: {...}},
        }
        archivos_por_ficha[ficha] = {nombres de archivo que la contienen}
    """

    def __init__(self):
        self.indice: dict = {}
        self.archivos_por_ficha: dict = {}
        self.archivos_omitidos: list = []
        self.hojas_leidas = 0
        self.filas_leidas = 0
        self.filas_descartadas = 0
        self.claves_duplicadas = 0
        self.discrepancias_etiqueta = 0
        self.etiquetas_reporte_comparadas = 0


def obtener_excels_bd(carpeta_bd: Path) -> list:
    if not carpeta_bd.exists():
        raise FileNotFoundError(f"No existe la carpeta con los Excels de la BD: {carpeta_bd}")
    excels = [
        a for a in carpeta_bd.rglob("*")
        if a.is_file()
        and a.suffix.lower() in (".xlsx", ".xlsm")
        and not a.name.startswith("~$")
    ]
    return sorted(set(excels), key=lambda p: str(p).lower())


def _mapa_encabezados_export(fila_valores) -> dict:
    """{encabezado_normalizado: índice 0-based}. Si se repite, gana el primero."""
    mapa = {}
    for i, v in enumerate(fila_valores):
        t = normalizar_texto(v)
        if t and t not in mapa:
            mapa[t] = i
    return mapa


def _localizar_encabezado_export(ws):
    """Devuelve (numero_fila, mapa) de la primera fila con 'GRUPO' e 'ID RAP'."""
    for numero, fila in enumerate(
        ws.iter_rows(min_row=1, max_row=FILA_ENCABEZADO_EXPORT_MAX, values_only=True),
        start=1,
    ):
        mapa = _mapa_encabezados_export(fila)
        if EXP_GRUPO in mapa and EXP_ID_RAP in mapa:
            return numero, mapa
    raise ExportNoReconocido(
        f"no se encontró una fila de encabezado con 'GRUPO' e 'ID RAP' en las "
        f"primeras {FILA_ENCABEZADO_EXPORT_MAX} filas"
    )


def _validar_columnas_export(mapa: dict):
    faltan = [
        c for c in (EXP_DOC_APRENDIZ, EXP_COMPETENCIA, EXP_RAP)
        if c not in mapa
    ]
    if faltan:
        raise ExportNoReconocido("faltan columnas obligatorias: " + ", ".join(faltan))
    if EXP_DOC_JUICIO not in mapa and EXP_NOM_JUICIO not in mapa:
        raise ExportNoReconocido(
            "no hay columna del instructor que emitió el juicio "
            "(DOCUMENTO INSTRUCTOR / NOMBRE INSTRUCTOR EMITIÓ JUICIO): "
            "no se puede saber si el RAP está evaluado"
        )
    if EXP_DOC_PROG not in mapa and EXP_NOM_PROG not in mapa:
        raise ExportNoReconocido(
            "no hay columna del instructor programado "
            "(DOCUMENTO INSTRUCTOR PROGRAMADO / NOMBRE INSTRUCTOR PROGRAMADO): "
            "no se puede saber si el RAP tiene programación"
        )


def _leer_hoja_export(ws, nombre_archivo: str, base: BaseProgramacion):
    try:
        ws.reset_dimensions()  # exports con dimensión mal declarada
    except Exception:
        pass

    fila_enc, mapa = _localizar_encabezado_export(ws)
    _validar_columnas_export(mapa)

    def col(nombre):
        return mapa.get(nombre)

    def valor(fila, nombre):
        i = col(nombre)
        return fila[i] if i is not None and i < len(fila) else None

    for numero, fila in enumerate(
        ws.iter_rows(min_row=fila_enc + 1, values_only=True), start=fila_enc + 1
    ):
        if not fila or all(v is None or _texto_celda(v) == "" for v in fila):
            continue
        base.filas_leidas += 1

        ficha = _digitos(valor(fila, EXP_GRUPO))
        documento = _digitos(valor(fila, EXP_DOC_APRENDIZ))
        competencia_original = _texto_celda(valor(fila, EXP_COMPETENCIA))
        competencia = normalizar_texto(competencia_original)
        id_rap = _texto_celda(valor(fila, EXP_ID_RAP))
        rap_texto = _texto_celda(valor(fila, EXP_RAP))

        if not ficha or not documento or not competencia or not (id_rap or rap_texto):
            base.filas_descartadas += 1
            continue

        doc_juicio = valor(fila, EXP_DOC_JUICIO)
        nom_juicio = valor(fila, EXP_NOM_JUICIO)
        doc_prog = valor(fila, EXP_DOC_PROG)
        nom_prog = valor(fila, EXP_NOM_PROG)

        evaluado = _instructor_valido(nom_juicio, doc_juicio)
        programado = _instructor_valido(nom_prog, doc_prog)
        caso = clasificar_rap(evaluado, programado)

        # Solo se muestra como instructor el que cuenta. Si la celda traía un
        # marcador (p. ej. "PENDIENTE POR ASIGNAR") se conserva aparte, como
        # nota, para que el detalle no contradiga el caso asignado.
        instructor_juicio = _etiqueta_instructor(nom_juicio, doc_juicio)
        instructor_prog = _etiqueta_instructor(nom_prog, doc_prog)
        notas = []
        if not evaluado and instructor_juicio:
            notas.append(f"Juicio ignorado: {instructor_juicio}")
            instructor_juicio = ""
        if not programado and instructor_prog:
            notas.append(f"Programación ignorada: {instructor_prog}")
            instructor_prog = ""

        # Control cruzado: si el propio export trae una de las 4 etiquetas,
        # se compara con la derivada. Solo se cuenta y se registra.
        etiqueta_reporte = _CASOS_NORMALIZADOS.get(
            normalizar_texto(valor(fila, EXP_ETIQUETA_REPORTE))
        )
        if etiqueta_reporte is not None:
            base.etiquetas_reporte_comparadas += 1
            if etiqueta_reporte != caso:
                base.discrepancias_etiqueta += 1
                logger.warning(
                    "%s fila %d: el export dice '%s' pero las columnas de "
                    "instructor indican '%s' (se usa la derivada de las columnas).",
                    nombre_archivo, numero, etiqueta_reporte, caso,
                )

        comps = base.indice.setdefault((ficha, documento), {})
        entrada = comps.setdefault(competencia, {
            "competencia": competencia_original,
            "id_competencia": _texto_celda(valor(fila, EXP_ID_COMPETENCIA)),
            "raps": {},
        })

        llave_rap = id_rap or normalizar_texto(rap_texto)
        if llave_rap in entrada["raps"]:
            base.claves_duplicadas += 1   # se conserva la primera aparición
            continue

        entrada["raps"][llave_rap] = {
            "id_rap": id_rap,
            "rap": rap_texto,
            "caso": caso,
            "evaluado": evaluado,
            "programado": programado,
            "instructor_juicio": instructor_juicio,
            "instructor_programado": instructor_prog,
            "notas": notas,
            "origen": f"{nombre_archivo} [{ws.title}] fila {numero}",
            "archivo": nombre_archivo,
        }
        base.archivos_por_ficha.setdefault(ficha, set()).add(nombre_archivo)


def cargar_base_programacion(
    carpeta_bd: Path, progress_callback: ProgressCallback = None
) -> BaseProgramacion:
    excels = obtener_excels_bd(carpeta_bd)
    if not excels:
        raise FileNotFoundError(f"No hay Excels (.xlsx/.xlsm) en: {carpeta_bd}")

    base = BaseProgramacion()
    total = len(excels)
    t0 = time.time()

    for n, ruta in enumerate(excels, start=1):
        try:
            wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
        except Exception as e:
            logger.exception("No se pudo abrir %s", ruta.name)
            base.archivos_omitidos.append(
                {"archivo": ruta.name, "hoja": None, "motivo": f"No se pudo abrir: {e}"}
            )
            _notificar(progress_callback, f"Leyendo Excels de la BD: {n}/{total}…", n, total)
            continue

        try:
            for ws in wb.worksheets:
                try:
                    _leer_hoja_export(ws, ruta.name, base)
                    base.hojas_leidas += 1
                except ExportNoReconocido as e:
                    logger.warning("%s [%s] omitida: %s", ruta.name, ws.title, e)
                    base.archivos_omitidos.append(
                        {"archivo": ruta.name, "hoja": ws.title, "motivo": str(e)}
                    )
        finally:
            wb.close()

        _notificar(progress_callback, f"Leyendo Excels de la BD: {n}/{total}…", n, total)

    logger.info(
        "Base de programación: %d archivo(s), %d hoja(s) leída(s), %d fila(s) "
        "(%d descartada(s), %d clave(s) duplicada(s)), %d ficha(s), "
        "%d aprendiz-ficha, %.1fs",
        total, base.hojas_leidas, base.filas_leidas, base.filas_descartadas,
        base.claves_duplicadas, len(base.archivos_por_ficha),
        len(base.indice), time.time() - t0,
    )
    if base.etiquetas_reporte_comparadas:
        logger.info(
            "Control cruzado con la etiqueta del export: %d comparada(s), %d discrepancia(s)",
            base.etiquetas_reporte_comparadas, base.discrepancias_etiqueta,
        )
    if not base.indice:
        raise ValueError(
            "Ningún Excel de la Base de Datos tiene la estructura esperada del "
            "export (hoja con GRUPO, ID RAP, instructor que emitió juicio e "
            "instructor programado). Detalle: "
            + "; ".join(f"{o['archivo']} [{o['hoja']}]: {o['motivo']}"
                        for o in base.archivos_omitidos[:5])
        )
    return base


# ============================================================
# CRUCE CONSOLIDADO <-> BASE
# ============================================================

def buscar_competencia(base: BaseProgramacion, ficha: str, documento: str, frase: str, codigo=None):
    """
    Devuelve (resultado, entrada|None, tipo_match|None) con resultado en
    "OK", "SIN_DOCUMENTO", "SIN_FICHA", "SIN_APRENDIZ" o "SIN_COMPETENCIA".
    """
    if not documento:
        return "SIN_DOCUMENTO", None, None
    if not ficha or ficha not in base.archivos_por_ficha:
        return "SIN_FICHA", None, None
    comps = base.indice.get((ficha, documento))
    if not comps:
        return "SIN_APRENDIZ", None, None

    if frase in comps:
        return "OK", comps[frase], "EXACTA"

    if codigo:
        codigo = _digitos(codigo)
        for entrada in comps.values():
            if codigo and _digitos(entrada["id_competencia"]) == codigo:
                return "OK", entrada, "POR CÓDIGO"

    mejor, mejor_score = None, 0.0
    for nombre in comps:
        score = fuzz.ratio(frase, nombre) / 100.0
        if score > mejor_score:
            mejor, mejor_score = nombre, score
    if mejor is not None and mejor_score >= UMBRAL_COMPETENCIA:
        return "OK", comps[mejor], f"SIMILAR ({mejor_score:.0%})"
    return "SIN_COMPETENCIA", None, None


def _resumir_fila(base_resultado: str, entrada, tipo_match):
    """Arma el resultado de UNA fila del Consolidado."""
    if base_resultado != "OK":
        return {
            "estado": ETIQUETA_SIN_COINCIDENCIA,
            "orden": ORDEN_SIN_COINCIDENCIA,
            "observacion": MOTIVOS_SIN_COINCIDENCIA[base_resultado],
            "archivos": [],
            "detalle": [],
            "raps": [],
            "coincidencia": None,
            "motivo": base_resultado,
            "conteo": Counter(),
        }

    raps = list(entrada["raps"].values())
    conteo = Counter(r["caso"] for r in raps)

    if len(conteo) == 1:
        estado = next(iter(conteo))
    else:
        # Los RAP de la competencia no comparten el mismo caso: se listan
        # los casos presentes (siguen siendo solo los 4 casos) con su cantidad.
        estado = "\n".join(f"{c} ({conteo[c]})" for c in CASOS if c in conteo)

    n = len(raps)
    evaluados = sum(1 for r in raps if r["evaluado"])
    programados = sum(1 for r in raps if r["programado"])
    observacion = (
        f"{n} RAP en BD, competencia {tipo_match}: {evaluados}/{n} evaluado(s), "
        f"{programados}/{n} con programación."
    )

    detalle = []
    for r in raps:
        linea = f"{r['id_rap'] or '?'} | {r['caso']}"
        if r["instructor_juicio"]:
            linea += f" | Juicio: {r['instructor_juicio']}"
        if r["instructor_programado"]:
            linea += f" | Programado: {r['instructor_programado']}"
        if r["notas"]:
            linea += " | " + "; ".join(r["notas"])
        detalle.append(linea)

    return {
        "estado": estado,
        "orden": min(ORDEN_CASO[c] for c in conteo),
        "observacion": observacion,
        "archivos": sorted({r["archivo"] for r in raps}),
        "detalle": detalle,
        "raps": raps,
        "coincidencia": tipo_match,
        "motivo": None,
        "conteo": conteo,
    }


def analizar_ficha(ficha: str, filas_validas, base: BaseProgramacion) -> dict:
    """
    filas_validas: lista de (fila, codigo, frase, valor_original, documento).
    Devuelve {fila: resultado_de_la_fila}.
    """
    ficha_digitos = _ficha_de_titulo(ficha)
    resultados = {}
    for fila, codigo, frase, valor_original, documento in filas_validas:
        res, entrada, tipo = buscar_competencia(
            base, ficha_digitos, _digitos(documento), frase, codigo
        )
        resultados[fila] = _resumir_fila(res, entrada, tipo)
    return resultados


# ============================================================
# FORMATO DE LAS COLUMNAS DE SALIDA (hereda el diseño de la tabla)
# ============================================================

def _lineas_estimadas(texto, ancho) -> int:
    """Líneas que ocupa `texto` con wrap en una columna de `ancho` caracteres."""
    if texto is None:
        return 1
    por_linea = max(1, int((ancho or ANCHO_DEFECTO_COL) * 1.1))
    return max(1, sum(
        max(1, math.ceil(len(parrafo) / por_linea))
        for parrafo in str(texto).split("\n")
    ))


def _copiar_estilo(origen, destino):
    destino.font = copy(origen.font)
    destino.fill = copy(origen.fill)
    destino.border = copy(origen.border)
    destino.protection = copy(origen.protection)
    destino.number_format = "General"


def _extender_tablas_y_filtros(ws, cols: "ColumnasHoja"):
    """
    Si el Consolidado usa una Tabla de Excel (ListObject) o un autofiltro que
    llega hasta la última columna de la tabla, se extiende para incluir las
    columnas de salida: así heredan estilo de tabla, bandas y filtros.
    """
    ultima_salida = max(cols.salida.values())
    if ultima_salida <= cols.ultima_col_tabla:
        return
    letra = get_column_letter

    for tabla in ws.tables.values():
        c1, r1, c2, r2 = range_boundaries(tabla.ref)
        if r1 != cols.fila_encabezado or c2 < cols.ultima_col_tabla or c2 >= ultima_salida:
            continue
        siguiente_id = max((tc.id for tc in tabla.tableColumns), default=0) + 1
        for c in range(c2 + 1, ultima_salida + 1):
            nombre = ws.cell(cols.fila_encabezado, c).value
            nombre = str(nombre) if nombre not in (None, "") else f"Columna{c}"
            tabla.tableColumns.append(TableColumn(id=siguiente_id, name=nombre))
            siguiente_id += 1
        tabla.ref = f"{letra(c1)}{r1}:{letra(ultima_salida)}{r2}"
        if tabla.autoFilter is not None:
            tabla.autoFilter.ref = tabla.ref

    filtro = ws.auto_filter.ref
    if filtro:
        c1, r1, c2, r2 = range_boundaries(filtro)
        if r1 == cols.fila_encabezado and cols.ultima_col_tabla <= c2 < ultima_salida:
            ws.auto_filter.ref = f"{letra(c1)}{r1}:{letra(ultima_salida)}{r2}"


def aplicar_formato_salida(ws, cols: "ColumnasHoja", ultima_fila: int):
    """
    Da a las columnas de salida el mismo diseño que la tabla del Consolidado
    (fuente, relleno y bordes de la columna de competencia, fila por fila, así
    se conservan las bandas) y controla el alto de fila:

      * wrap de texto en las columnas nuevas, con ancho fijo;
      * la fila crece como máximo MAX_LINEAS_FILA líneas por las columnas
        nuevas (respetando lo que ya necesitaban las columnas originales).

    Se llama DESPUÉS de reordenar, para que el alto corresponda al contenido
    final de cada fila.
    """
    anchos = {clave: ancho for clave, _enc, ancho in COLUMNAS_SALIDA}

    # Encabezado
    ref_enc = ws.cell(cols.fila_encabezado, cols.col_estilo)
    for clave, col in cols.salida.items():
        celda = ws.cell(cols.fila_encabezado, col)
        _copiar_estilo(ref_enc, celda)
        celda.alignment = Alignment(
            horizontal=ref_enc.alignment.horizontal,
            vertical=ref_enc.alignment.vertical or "center",
            wrap_text=True,
        )
        if clave in cols.creadas:
            ws.column_dimensions[get_column_letter(col)].width = anchos[clave]

    # Filas de datos
    for fila in range(cols.fila_encabezado + 1, ultima_fila + 1):
        ref = ws.cell(fila, cols.col_estilo)
        lineas_nuevas = 1
        for clave, col in cols.salida.items():
            celda = ws.cell(fila, col)
            _copiar_estilo(ref, celda)
            celda.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            ancho = ws.column_dimensions[get_column_letter(col)].width or anchos[clave]
            lineas_nuevas = max(lineas_nuevas, _lineas_estimadas(celda.value, ancho))

        lineas_originales = 1
        for c in range(1, cols.ultima_col_tabla + 1):
            celda = ws.cell(fila, c)
            if celda.alignment is not None and celda.alignment.wrap_text and isinstance(celda.value, str):
                ancho = ws.column_dimensions[get_column_letter(c)].width
                lineas_originales = max(lineas_originales, _lineas_estimadas(celda.value, ancho))

        lineas = max(lineas_originales, min(lineas_nuevas, MAX_LINEAS_FILA))
        if lineas > 1:
            actual = ws.row_dimensions[fila].height or 0
            ws.row_dimensions[fila].height = max(actual, lineas * ALTO_LINEA_PT)


# ============================================================
# REORDENAR FILAS + HOJA DE REPORTE
# ============================================================

def reordenar_filas_por_estado(ws, orden_por_fila, cols: "ColumnasHoja"):
    """
    Dentro del bloque de filas de cada aprendiz (mismo documento), ordena
    por `orden_por_fila` (índice del caso; ver ORDEN_CASO). El orden es
    estable: a igual caso se conserva el orden original.
    """
    filas = sorted(orden_por_fila.keys())
    if len(filas) < 2:
        return

    max_col = ws.max_column

    def capturar_fila(fila):
        celdas = []
        for c in range(1, max_col + 1):
            celda = ws.cell(fila, c)
            celdas.append({
                "valor": celda.value, "fill": copy(celda.fill), "font": copy(celda.font),
                "border": copy(celda.border), "alignment": copy(celda.alignment),
                "number_format": celda.number_format, "protection": copy(celda.protection),
            })
        return celdas

    def aplicar_fila(fila, datos_fila):
        for c in range(1, max_col + 1):
            celda = ws.cell(fila, c)
            datos = datos_fila[c - 1]
            celda.value = datos["valor"]
            celda.fill = datos["fill"]
            celda.font = datos["font"]
            celda.border = datos["border"]
            celda.alignment = datos["alignment"]
            celda.number_format = datos["number_format"]
            celda.protection = datos["protection"]

    bloques = []
    bloque_actual = [filas[0]]
    doc_actual = ws.cell(filas[0], cols.documento).value
    for fila in filas[1:]:
        doc = ws.cell(fila, cols.documento).value
        if doc == doc_actual:
            bloque_actual.append(fila)
        else:
            bloques.append(bloque_actual)
            bloque_actual = [fila]
            doc_actual = doc
    bloques.append(bloque_actual)

    for bloque in bloques:
        if len(bloque) < 2:
            continue
        snapshots = {f: capturar_fila(f) for f in bloque}
        orden_nuevo = sorted(bloque, key=lambda f: orden_por_fila[f])
        for posicion, fila_origen in zip(bloque, orden_nuevo):
            aplicar_fila(posicion, snapshots[fila_origen])


def escribir_hoja_reporte(wb, datos_fichas, resultados_globales):
    """Hoja 'Reporte': una fila por RAP (o una sola fila si no hubo coincidencia)."""
    if "Reporte" in wb.sheetnames:
        del wb["Reporte"]
    ws = wb.create_sheet("Reporte")

    ws.append([
        "Ficha", "Fila", "Código", "Competencia (Excel)", "ID RAP", "RAP", "Caso",
        "Instructor que emitió juicio", "Instructor programado",
        "Coincidencia competencia", "Observación", "Archivo BD / hoja / fila",
    ])

    for ficha, filas_validas in datos_fichas.items():
        resultados = resultados_globales.get(ficha, {})
        for fila, codigo, frase, valor_original, documento in filas_validas:
            res = resultados.get(fila)
            if res is None:
                continue
            if not res["raps"]:
                ws.append([
                    ficha, fila, codigo or "", valor_original, "", "",
                    res["estado"], "", "", "", res["observacion"], "",
                ])
                continue
            for r in res["raps"]:
                ws.append([
                    ficha, fila, codigo or "", valor_original, r["id_rap"], r["rap"],
                    r["caso"], r["instructor_juicio"], r["instructor_programado"],
                    res["coincidencia"], res["observacion"], r["origen"],
                ])

    anchos = {"A": 14, "B": 8, "C": 12, "D": 40, "E": 10, "F": 55, "G": 36,
              "H": 32, "I": 32, "J": 20, "K": 45, "L": 45}
    for columna, ancho in anchos.items():
        ws.column_dimensions[columna].width = ancho

    # Encabezado con estilo propio, filtros y primera fila fija.
    borde = Side(style="thin", color="808080")
    for celda in ws[1]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="1F4E78")
        celda.border = Border(left=borde, right=borde, top=borde, bottom=borde)
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"

    # Texto con wrap, pero la fila no crece más de MAX_LINEAS_FILA líneas.
    columnas_texto = (4, 6, 7, 8, 9, 11, 12)
    for fila in range(2, ws.max_row + 1):
        lineas = 1
        for col in range(1, ws.max_column + 1):
            celda = ws.cell(fila, col)
            celda.border = Border(left=borde, right=borde, top=borde, bottom=borde)
            if col in columnas_texto:
                celda.alignment = Alignment(wrap_text=True, vertical="top")
                ancho = anchos[get_column_letter(col)]
                lineas = max(lineas, _lineas_estimadas(celda.value, ancho))
            else:
                celda.alignment = Alignment(vertical="top")
        lineas = min(lineas, MAX_LINEAS_FILA)
        if lineas > 1:
            ws.row_dimensions[fila].height = lineas * ALTO_LINEA_PT


# ============================================================
# PROCESO PRINCIPAL
# ============================================================

def procesar(
    archivo_excel: Path,
    carpeta_bd: Path,
    salida_dir: Path,
    progress_callback: ProgressCallback = None,
) -> dict:
    t_inicio = time.time()
    logger.info("=== INICIO procesar() ===")
    logger.info("Consolidado: %s", archivo_excel)
    logger.info("Carpeta BD (Excels): %s", carpeta_bd)
    logger.info("Salida: %s", salida_dir)

    if not archivo_excel.exists():
        raise FileNotFoundError(f"No se encontró el Excel: {archivo_excel}")

    salida_dir.mkdir(parents=True, exist_ok=True)

    # 1) Base de programación (Excels de la BD).
    _notificar(progress_callback, "Leyendo Excels de la Base de Datos…", 0, 1)
    base = cargar_base_programacion(carpeta_bd, progress_callback)

    # 2) Consolidado: detectar columnas y filas a evaluar.
    _notificar(progress_callback, "Abriendo el Consolidado…", 0, 1)
    t = time.time()
    wb = openpyxl.load_workbook(archivo_excel)
    logger.info("Consolidado abierto en %.1fs (%d hojas)", time.time() - t, len(wb.worksheets))

    datos_fichas = {}
    columnas_por_ficha = {}
    fichas_omitidas_estructura = []
    for ws in wb.worksheets:
        ficha = str(ws.title).strip()
        if ficha.lower() == "reporte":
            continue  # hoja generada por una ejecución anterior; se recrea al final
        try:
            cols = detectar_columnas(ws)
        except ColumnasNoDetectadas as e:
            logger.warning("Hoja '%s' omitida: %s", ficha, e)
            fichas_omitidas_estructura.append({"ficha": ficha, "motivo": str(e)})
            continue
        columnas_por_ficha[ficha] = cols
        logger.info("Hoja '%s': columnas detectadas -> %r", ficha, cols)

        filas_validas = []
        for fila in range(cols.fila_encabezado + 1, ws.max_row + 1):
            if not fila_es_valida(ws, fila, cols):
                continue
            valor_original = ws.cell(fila, cols.competencia).value
            codigo, frase = extraer_competencia(valor_original)
            if frase:
                documento = ws.cell(fila, cols.documento).value
                filas_validas.append((fila, codigo, frase, valor_original, documento))
        datos_fichas[ficha] = filas_validas

    if fichas_omitidas_estructura:
        logger.warning(
            "%d hoja(s) omitida(s) por estructura no reconocida: %s",
            len(fichas_omitidas_estructura),
            [f["ficha"] for f in fichas_omitidas_estructura],
        )

    total_fichas = len(datos_fichas)
    fichas_con_bd = [f for f in datos_fichas if _ficha_de_titulo(f) in base.archivos_por_ficha]
    fichas_sin_bd = [f for f in datos_fichas if f not in fichas_con_bd]
    logger.info(
        "Fichas (hojas) con filas válidas: %d — con datos en BD: %d, sin datos: %d",
        total_fichas, len(fichas_con_bd), len(fichas_sin_bd),
    )

    # 3) Cruce.
    _notificar(progress_callback, f"Cruzando {total_fichas} fichas con la BD…", 0, total_fichas)
    t = time.time()
    resultados_globales = {}
    for n, (ficha, filas_validas) in enumerate(datos_fichas.items(), start=1):
        try:
            resultados_globales[ficha] = analizar_ficha(ficha, filas_validas, base)
        except Exception:
            logger.exception("Error en ficha %s", ficha)
            resultados_globales[ficha] = {}
        if n % 5 == 0 or n == total_fichas:
            _notificar(
                progress_callback,
                f"Ficha {ficha} cruzada ({n}/{total_fichas})…", n, total_fichas,
            )
    logger.info("Cruce: %.1fs", time.time() - t)

    # 4) Escritura en el Consolidado + totales.
    total_excels_bd = len(obtener_excels_bd(carpeta_bd))
    raps_por_caso = Counter()
    total_programado = total_no_programado = total_parcial = total_sin_coincidencia = 0
    total_filas = 0

    for ws in wb.worksheets:
        ficha = str(ws.title).strip()
        cols = columnas_por_ficha.get(ficha)
        if cols is None:
            continue  # hoja omitida: se deja intacta
        resultados = resultados_globales.get(ficha, {})

        for clave, encabezado, _ancho in COLUMNAS_SALIDA:
            ws.cell(cols.fila_encabezado, cols.salida[clave]).value = encabezado
        _extender_tablas_y_filtros(ws, cols)

        orden_por_fila = {}
        for fila in range(cols.fila_encabezado + 1, ws.max_row + 1):
            for col in cols.salida.values():
                ws.cell(fila, col).value = None
            res = resultados.get(fila)
            if res is None:
                continue

            ws.cell(fila, cols.estado).value = res["estado"]
            ws.cell(fila, cols.observacion).value = res["observacion"]
            ws.cell(fila, cols.archivos).value = "\n".join(res["archivos"]) or None
            ws.cell(fila, cols.detalle).value = "\n".join(res["detalle"]) or None

            orden_por_fila[fila] = res["orden"]
            total_filas += 1
            raps_por_caso.update(res["conteo"])

            if res["motivo"] is not None:
                total_sin_coincidencia += 1
            else:
                con_prog = sum(1 for r in res["raps"] if r["programado"])
                if con_prog == len(res["raps"]):
                    total_programado += 1
                elif con_prog == 0:
                    total_no_programado += 1
                else:
                    total_parcial += 1

        reordenar_filas_por_estado(ws, orden_por_fila, cols)
        aplicar_formato_salida(
            ws, cols, max(orden_por_fila, default=cols.fila_encabezado)
        )

    _notificar(progress_callback, "Generando hoja de reporte…", total_fichas, total_fichas)
    escribir_hoja_reporte(wb, datos_fichas, resultados_globales)

    ruta_salida = salida_dir / "Consolidado_Procesado.xlsx"
    wb.save(ruta_salida)

    logger.info(
        "RESUMEN | total=%.1fs | fichas=%d | excels_bd=%d | filas=%d | "
        "programado=%d | no_programado=%d | parcial=%d | sin_coincidencia=%d | raps=%s",
        time.time() - t_inicio, total_fichas, total_excels_bd,
        total_filas, total_programado, total_no_programado, total_parcial,
        total_sin_coincidencia, dict(raps_por_caso),
    )
    _notificar(progress_callback, "Listo.", total_fichas, total_fichas)

    return {
        "archivo_generado": str(ruta_salida),
        "total_fichas": total_fichas,
        "total_filas": total_filas,
        "total_excels_bd": total_excels_bd,
        "fichas_con_bd": fichas_con_bd,
        "fichas_sin_bd": fichas_sin_bd,
        "fichas_omitidas_estructura": fichas_omitidas_estructura,
        # Por fila del Consolidado (una competencia de un aprendiz):
        #   programado    = TODOS sus RAP tienen programación
        #   no_programado = NINGÚN RAP tiene programación
        #   parcial       = algunos sí y otros no
        "total_programado": total_programado,
        "total_no_programado": total_no_programado,
        "total_parcial": total_parcial,
        "total_sin_coincidencia": total_sin_coincidencia,
        # Por RAP (cada RAP cruzado cuenta una vez):
        "raps_por_caso": {caso: raps_por_caso.get(caso, 0) for caso in CASOS},
        "mapa_ficha_archivos": {
            ficha: sorted(archivos)
            for ficha, archivos in base.archivos_por_ficha.items()
        },
        "archivos_bd_omitidos": base.archivos_omitidos,
        "filas_bd_leidas": base.filas_leidas,
        "filas_bd_descartadas": base.filas_descartadas,
        "claves_bd_duplicadas": base.claves_duplicadas,
        "discrepancias_etiqueta_export": base.discrepancias_etiqueta,
    }