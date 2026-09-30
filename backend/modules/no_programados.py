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
import os
import re
import time
import unicodedata
from collections import Counter
from copy import copy
from pathlib import Path
from typing import Callable, Optional

import openpyxl
from openpyxl.styles import Alignment
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

    siguiente_col_libre = max_col_original + 1

    def _columna_salida(encabezado_texto):
        nonlocal siguiente_col_libre
        col = _buscar_columna_por_texto_exacto(
            ws, fila_encabezado, encabezado_texto, max_col_original
        )
        if col is not None:
            return col
        col = siguiente_col_libre
        siguiente_col_libre += 1
        return col

    cols.estado = _columna_salida(ENCABEZADO_ESTADO)
    cols.observacion = _columna_salida(ENCABEZADO_OBSERVACION)
    cols.archivos = _columna_salida(ENCABEZADO_ARCHIVOS)
    cols.detalle = _columna_salida(ENCABEZADO_DETALLE)

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

    for fila in range(2, ws.max_row + 1):
        for col in (4, 6, 8, 9, 11, 12):
            ws.cell(fila, col).alignment = Alignment(wrap_text=True, vertical="top")

    anchos = {"A": 14, "B": 8, "C": 12, "D": 40, "E": 10, "F": 55, "G": 36,
              "H": 32, "I": 32, "J": 20, "K": 45, "L": 45}
    for columna, ancho in anchos.items():
        ws.column_dimensions[columna].width = ancho


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

        ws.cell(cols.fila_encabezado, cols.estado).value = ENCABEZADO_ESTADO
        ws.cell(cols.fila_encabezado, cols.observacion).value = ENCABEZADO_OBSERVACION
        ws.cell(cols.fila_encabezado, cols.archivos).value = ENCABEZADO_ARCHIVOS
        ws.cell(cols.fila_encabezado, cols.detalle).value = ENCABEZADO_DETALLE

        orden_por_fila = {}
        for fila in range(cols.fila_encabezado + 1, ws.max_row + 1):
            for col in (cols.observacion, cols.archivos, cols.detalle):
                ws.cell(fila, col).value = None
            res = resultados.get(fila)
            if res is None:
                continue

            ws.cell(fila, cols.estado).value = res["estado"]
            ws.cell(fila, cols.observacion).value = res["observacion"]
            ws.cell(fila, cols.archivos).value = "\n".join(res["archivos"]) or None
            ws.cell(fila, cols.detalle).value = "\n".join(res["detalle"]) or None
            for col in (cols.estado, cols.observacion, cols.archivos, cols.detalle):
                celda = ws.cell(fila, col)
                if isinstance(celda.value, str) and "\n" in celda.value:
                    celda.alignment = Alignment(wrap_text=True, vertical="top")

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