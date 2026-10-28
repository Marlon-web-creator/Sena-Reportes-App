"""
modules/actas_resoluciones.py

Cruza un CONSOLIDADO (Excel con aprendices) contra los PDFs de actas y
resoluciones que están en la sección "Base de Datos" con módulo
"Actas y Resoluciones", y agrega DOS columnas nuevas a cada tabla del
consolidado:

    ESTADO REAL                      estado del aprendiz después de revisar los PDFs
    OBSERVACIÓN ACTAS/RESOLUCIONES   qué se encontró y DÓNDE (archivo, página y fragmento)

Las columnas se generan "proceduralmente": no se asume ninguna posición ni
nombre fijo de columna.

    * Por cada hoja se busca la fila de encabezado (en las primeras filas) que
      tenga una columna de documento ("Número de Documento", "Documento",
      "Identificación", "Cédula"...). Las filas informativas de arriba
      ("FICHA: ...", "PROGRAMA: ...") y las hojas sin columna de documento
      (p. ej. la hoja "Reporte") se ignoran / se omiten con aviso.
    * Si la hoja tiene una columna de estado ("Estado", "Estado del aprendiz")
      se usa como estado base; si no, el estado base queda vacío.
    * Si hay columnas de nombres/apellidos se usan como respaldo para hallar al
      aprendiz por nombre cuando el PDF no trae su documento.
    * Las dos columnas nuevas se agregan a la derecha de la última columna del
      encabezado. Si el archivo ya las trae (reproceso), se sobrescriben.

Cómo se busca al aprendiz en los PDFs (por página):

    1. Por número de documento (soporta 1011323490, 1.011.323.490, 1 011 323 490).
    2. Respaldo por nombre: al menos 3 palabras del nombre en la MISMA línea
       (se marca "hallado por nombre, verificar").

Cómo se decide qué dice el PDF de ese aprendiz:

    Se toma una ventana de líneas alrededor de donde aparece (ANTES/DESPUÉS),
    cortada donde empieza OTRO aprendiz del consolidado, para que la decisión
    de una fila de una tabla no se contagie a la fila vecina. En esa ventana se
    buscan los términos de cada categoría (ver CATEGORIAS). Si la ventana no
    trae ninguno, se mira el título del documento (primeras líneas de la
    página 1). Si tampoco, el aprendiz solo queda como "mencionado".

    CANCELACIÓN  -> ESTADO REAL = "EN RESOLUCIÓN DE CANCELACIÓN"
    CONDICIONADO -> ESTADO REAL = "CONDICIONADO"
    Si aparecen ambos, gana CANCELACIÓN (y la observación lista los dos).

Casos que NO cambian el estado y quedan marcados "REVISAR":
    * el fragmento habla de levantar / revocar / dejar sin efecto / archivar;
    * la cancelación aparece solo como advertencia condicional ("en caso de
      incumplimiento...") y no hay condicionamiento.
    * el consolidado ya lo tiene como CANCELADO/RETIRADO/etc. y el PDF dice otra
      cosa.

PDFs escaneados: si la página no tiene texto se intenta OCR (pytesseract +
pypdfium2, opcional). Si no está disponible, la página se cuenta como "sin
texto" y se avisa en el resumen.

Solo se leen PDFs. Otros archivos con el mismo módulo se ignoran.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ProgressCallback = Optional[Callable[[str, int, int], None]]

EXTENSIONES_PDF = (".pdf",)
EXTENSIONES_CONSOLIDADO = (".xlsx", ".xlsm")

# ------------------------------------------------------------
# Configuración (ajustable sin tocar la lógica)
# ------------------------------------------------------------
COL_ESTADO_REAL = "ESTADO REAL"
COL_OBSERVACION = "OBSERVACIÓN ACTAS/RESOLUCIONES"

MAX_FILAS_ENCABEZADO = 40        # filas a revisar buscando el encabezado de la tabla
VENTANA_ANTES = 2                # líneas antes de donde aparece el aprendiz
VENTANA_DESPUES = 4              # líneas después
LINEAS_TITULO = 15               # líneas de la pág. 1 que se toman como "título"
LARGO_FRAGMENTO = 220            # caracteres del fragmento citado
MAX_EVIDENCIAS_EN_OBSERVACION = 3
MIN_CARACTERES_PAGINA = 30       # menos que esto = página sin texto (¿escaneada?)
OCR_ACTIVO = (os.environ.get("AR_OCR", "1") != "0")
OCR_MAX_PAGINAS_POR_PDF = int(os.environ.get("AR_OCR_MAX_PAGINAS") or 400)
OCR_ESCALA = float(os.environ.get("AR_OCR_ESCALA") or 2.5)      # resolución al rasterizar páginas
OCR_HILOS = int(os.environ.get("AR_OCR_HILOS") or min(4, os.cpu_count() or 2))
OCR_TIMEOUT_PAGINA = int(os.environ.get("AR_OCR_TIMEOUT") or 120)  # segundos por página
# Tesseract usa varios hilos por proceso; como OCR_HILOS ya paraleliza, se limita a 1.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

# ------------------------------------------------------------
# Categorías. Para agregar una (p. ej. "RETIRO VOLUNTARIO") basta con añadir
# una entrada aquí, en ESTADO_POR_CATEGORIA, ETIQUETA y PRIORIDAD.
# Los patrones se aplican sobre texto en MAYÚSCULAS y SIN TILDES.
# ------------------------------------------------------------
CANCELACION = "CANCELACION"
CONDICIONADO = "CONDICIONADO"
MENCIONADO = "MENCIONADO"  # aparece en el PDF pero sin términos de las demás

CATEGORIAS = {
    CANCELACION: re.compile(r"\bCANCEL\w*"),
    CONDICIONADO: re.compile(r"\bCONDICION(?:AD[OA]S?|AMIENTOS?|AR\w*|A)\b"),
}
ESTADO_POR_CATEGORIA = {
    CANCELACION: "EN RESOLUCIÓN DE CANCELACIÓN",
    CONDICIONADO: "CONDICIONADO",
}
ETIQUETA = {
    CANCELACION: "CANCELACIÓN",
    CONDICIONADO: "CONDICIONADO",
    MENCIONADO: "MENCIONADO",
}
PRIORIDAD = {CANCELACION: 0, CONDICIONADO: 1, MENCIONADO: 2}  # menor = más grave

# Lenguaje de levantamiento / revocatoria -> la evidencia no cambia el estado.
_RE_NEGACION = re.compile(
    r"\bLEVANT\w*|\bREVOC\w*|\bSIN EFECTO\b|\bNO SE (?:CANCEL|CONDICION)\w*"
    r"|\bARCHIV(?:AR|ESE|A|ADO|ADA)\b"
)
# Lenguaje de advertencia ("en caso de incumplimiento se cancelará...").
_RE_HIPOTETICO = re.compile(
    r"\bINCUMPL\w*|\bEN CASO DE\b|\bDE NO\b|\bSI NO\b|\bACARRE\w*|\bCONLLEV\w*"
    r"|\bADVERT\w*|\bPODRIA\b|\bPODRA\b|\bPOSIBLE\b"
)
# Si el consolidado ya trae uno de estos estados, el PDF no lo sobrescribe.
_ESTADO_BASE_TERMINAL = re.compile(r"CANCEL|RETIR|DESERC|CERTIF|EGRES|TRASLAD|GRADU|TITUL")

_PARTICULAS = {"DE", "DEL", "LA", "LAS", "LOS", "Y", "SAN", "SANTA"}

_RE_MILES = re.compile(r"(?<!\d)\d{1,3}(?:[., ]\d{3}){2,3}(?!\d)")
_RE_NUMERO = re.compile(r"(?<!\d)\d{6,11}(?!\d)")
_RE_PALABRA = re.compile(r"[A-Z]{2,}")

# Colores
_FILL_CANCELACION = PatternFill("solid", fgColor="F8CBAD")
_FILL_CONDICIONADO = PatternFill("solid", fgColor="FFF2CC")
_FILL_REVISAR = PatternFill("solid", fgColor="DDEBF7")
_BORDE = Border(*(Side(style="thin", color="BFBFBF"),) * 4)
_ENC_FILL = PatternFill("solid", fgColor="1F4E78")
_ENC_FONT = Font(bold=True, color="FFFFFF")


# ============================================================
# Utilidades
# ============================================================

def _notificar(cb: ProgressCallback, mensaje: str, actual: int, total: int) -> None:
    if cb:
        cb(mensaje, actual, total)


def _normalizar(texto) -> str:
    """Mayúsculas, sin tildes, espacios colapsados."""
    if texto is None:
        return ""
    s = unicodedata.normalize("NFKD", str(texto))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


def _norm_encabezado(valor) -> str:
    n = _normalizar(valor)
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]+", " ", n)).strip() if n else ""


def _normalizar_documento(valor) -> Optional[str]:
    """Documento como texto de solo dígitos (1012336732, 1012336732.0, '1.012.336.732')."""
    if valor is None:
        return None
    if isinstance(valor, float):
        if valor != valor:  # NaN
            return None
        if valor.is_integer():
            return str(int(valor))
    if isinstance(valor, int):
        return str(valor)
    s = str(valor).strip()
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    s = re.sub(r"\D", "", s)
    return s or None


def _limpiar_digitos(linea: str) -> str:
    """'1.011.323.490' / '1 011 323 490' -> '1011323490' (solo grupos de miles)."""
    return _RE_MILES.sub(lambda m: re.sub(r"[., ]", "", m.group(0)), linea)


def _clasificar(texto_norm: str) -> tuple[dict[str, list[str]], bool]:
    """
    Devuelve ({categoria: [términos]}, solo_condicional). `solo_condicional` es
    True cuando la única categoría hallada es una cancelación escrita como
    advertencia ("en caso de incumplimiento ... cancelación").
    """
    halladas = {}
    for cat, patron in CATEGORIAS.items():
        ms = list(patron.finditer(texto_norm))
        if ms:
            halladas[cat] = ms
    solo_condicional = False
    if CANCELACION in halladas:
        ms = halladas[CANCELACION]
        hip = [m for m in ms if _RE_HIPOTETICO.search(texto_norm[max(0, m.start() - 90):m.start()])]
        if len(hip) == len(ms):
            if len(halladas) > 1:
                del halladas[CANCELACION]
            else:
                solo_condicional = True
    return {c: sorted({m.group(0) for m in ms})[:3] for c, ms in halladas.items()}, solo_condicional


# ============================================================
# Consolidado: detección procedural de la tabla de cada hoja
# ============================================================

def _es_enc_documento(n: str) -> bool:
    if not n or re.search(r"\bTIPO\b", n):
        return False
    return bool(re.search(r"\b(DOCUMENTO|IDENTIFICACION|CEDULA)\b", n)) and len(n.split()) <= 5


def _es_enc_estado(n: str) -> bool:
    return bool(re.fullmatch(r"ESTADO(?: DEL| DE LA| DE)?(?: APRENDIZ| ESTUDIANTE| MATRICULA)?", n))


def _es_enc_nombre(n: str) -> bool:
    return bool(re.fullmatch(
        r"(?:NOMBRES?|APELLIDOS?|NOMBRE COMPLETO|NOMBRES? Y APELLIDOS|APELLIDOS Y NOMBRES"
        r"|(?:NOMBRES?|APELLIDOS?) (?:DEL )?APRENDIZ)", n))


@dataclass
class _Tabla:
    hoja: str
    fila_enc: int
    col_doc: int
    col_estado: Optional[int]
    cols_nombre: list[int]
    ultima_col: int                 # última columna "original" del encabezado
    col_estado_real: int
    col_obs: int
    nuevas: bool                    # False si el archivo ya traía las columnas
    filas: list[tuple[int, str, str, str]] = field(default_factory=list)  # (fila, doc, estado_base, nombre)


def _detectar_tabla(ws) -> Optional[_Tabla]:
    tope = min(MAX_FILAS_ENCABEZADO, ws.max_row or 0)
    if tope < 1:
        return None
    for r, valores in enumerate(ws.iter_rows(min_row=1, max_row=tope, values_only=True), start=1):
        enc = [(c, _norm_encabezado(v)) for c, v in enumerate(valores, start=1)]
        enc = [(c, n) for c, n in enc if n]
        if len(enc) < 3:
            continue
        col_doc = next((c for c, n in enc if _es_enc_documento(n)), None)
        if col_doc is None:
            continue

        n_er, n_obs = _norm_encabezado(COL_ESTADO_REAL), _norm_encabezado(COL_OBSERVACION)
        col_er = next((c for c, n in enc if n == n_er), None)
        col_obs = next((c for c, n in enc if n == n_obs), None)
        propias = {c for c in (col_er, col_obs) if c}
        originales = [(c, n) for c, n in enc if c not in propias]
        col_estado = next((c for c, n in originales if _es_enc_estado(n)), None)
        cols_nombre = [c for c, n in originales if _es_enc_nombre(n)]
        ultima = max(c for c, _ in originales)

        libre = max(c for c, _ in enc) + 1
        nuevas = not (col_er and col_obs)
        if not col_er:
            col_er, libre = libre, libre + 1
        if not col_obs:
            col_obs = libre

        t = _Tabla(ws.title, r, col_doc, col_estado, cols_nombre, ultima, col_er, col_obs, nuevas)
        for nfila, vals in enumerate(ws.iter_rows(min_row=r + 1, values_only=True), start=r + 1):
            if len(vals) < col_doc:
                continue
            doc = _normalizar_documento(vals[col_doc - 1])
            if not doc or len(doc) < 5:
                continue
            base = ""
            if col_estado and len(vals) >= col_estado and vals[col_estado - 1] is not None:
                base = str(vals[col_estado - 1]).strip()
            nombre = " ".join(
                str(vals[c - 1]).strip() for c in cols_nombre
                if len(vals) >= c and vals[c - 1] not in (None, "")
            )
            t.filas.append((nfila, doc, base, nombre))
        return t
    return None


# ============================================================
# PDFs
# ============================================================

@dataclass
class Evidencia:
    doc: str
    categoria: str            # CANCELACION | CONDICIONADO | MENCIONADO
    archivo: str
    pagina: int
    tipo_documento: str       # ACTA | RESOLUCIÓN | DOCUMENTO
    fragmento: str
    encontrado_por: str       # DOCUMENTO | NOMBRE
    desde_titulo: bool = False
    revisar: bool = False
    nota: str = ""
    terminos: str = ""


class _Indice:
    """Documentos (y palabras de nombre) a buscar dentro de los PDFs."""

    def __init__(self, nombres_por_doc: dict[str, str]):
        self.docs = set(nombres_por_doc)
        self.por_palabra: dict[str, list[str]] = defaultdict(list)
        self.minimo: dict[str, int] = {}
        for doc, nombre in nombres_por_doc.items():
            palabras = {p for p in _RE_PALABRA.findall(_normalizar(nombre))
                        if len(p) >= 3 and p not in _PARTICULAS}
            if len(palabras) < 2:
                continue
            self.minimo[doc] = min(3, len(palabras))
            for p in palabras:
                self.por_palabra[p].append(doc)


_OCR_LISTO = None  # None = sin probar; False = no disponible; tupla = disponible


def _preparar_ocr():
    """(pytesseract, None, idioma) si pytesseract + el binario tesseract funcionan; si no, False."""
    global _OCR_LISTO
    if _OCR_LISTO is not None:
        return _OCR_LISTO
    if not OCR_ACTIVO:
        _OCR_LISTO = False
        return False
    try:
        import pytesseract
        langs = set(pytesseract.get_languages(config=""))  # falla si no está el binario
        if "spa" in langs and "eng" in langs:
            lang = "spa+eng"
        elif "spa" in langs:
            lang = "spa"
        else:
            lang = "eng"
        _OCR_LISTO = (pytesseract, None, lang)
    except Exception:
        _OCR_LISTO = False
    return _OCR_LISTO


# ---- Lectores de texto digital. Se prueban en orden y se usa el primero que
# ---- devuelva texto; cada uno es opcional (si no está instalado, se salta).

def _texto_pdfplumber(ruta: Path) -> list[str]:
    import pdfplumber

    out: list[str] = []
    with pdfplumber.open(str(ruta)) as pdf:
        for page in pdf.pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:
                out.append("")
            try:
                page.flush_cache()
            except Exception:
                pass
    return out


def _texto_pypdfium2(ruta: Path) -> list[str]:
    import pypdfium2

    pdf = pypdfium2.PdfDocument(str(ruta))
    out: list[str] = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    t = tp.get_text_range() or ""
                finally:
                    tp.close()
            except Exception:
                t = ""
            out.append(t.replace("\r\n", "\n").replace("\r", "\n"))
            try:
                page.close()
            except Exception:
                pass
    finally:
        pdf.close()
    return out


def _texto_pypdf(ruta: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    lector = PdfReader(str(ruta))
    if getattr(lector, "is_encrypted", False):
        try:
            lector.decrypt("")
        except Exception:
            pass
    out: list[str] = []
    for p in lector.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:
            out.append("")
    return out


def _texto_fitz(ruta: Path) -> list[str]:
    import fitz  # PyMuPDF

    d = fitz.open(str(ruta))
    try:
        if getattr(d, "needs_pass", False):
            d.authenticate("")
        return [p.get_text("text") or "" for p in d]
    finally:
        d.close()


_EXTRACTORES = (
    ("pdfplumber", _texto_pdfplumber),
    ("pypdfium2", _texto_pypdfium2),
    ("pypdf", _texto_pypdf),
    ("pymupdf", _texto_fitz),
)


def _abrir_renderizador(ruta: Path):
    """(render(i) -> imagen PIL, cerrar) usando pypdfium2 o PyMuPDF; None si no hay ninguno."""
    try:
        import pypdfium2

        d = pypdfium2.PdfDocument(str(ruta))

        def render(i: int):
            return d[i].render(scale=OCR_ESCALA).to_pil()

        return render, d.close
    except Exception:
        pass
    try:
        import fitz
        from PIL import Image

        d = fitz.open(str(ruta))
        if getattr(d, "needs_pass", False):
            d.authenticate("")

        def render(i: int):
            pix = d[i].get_pixmap(matrix=fitz.Matrix(OCR_ESCALA, OCR_ESCALA), alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

        return render, d.close
    except Exception:
        return None


def _ocr_imagen(ocr, img) -> str:
    from PIL import ImageOps

    gris = ImageOps.autocontrast(img.convert("L"))
    return ocr[0].image_to_string(gris, lang=ocr[2], config="--psm 6", timeout=OCR_TIMEOUT_PAGINA)


def _texto_paginas(ruta: Path, est: dict) -> list[tuple[int, str]]:
    """
    Texto de cada página: primero la capa de texto (PDF digital) con el primer
    lector que funcione; las páginas que quedan sin texto (escaneadas / solo
    imagen) se pasan por OCR. Así un mismo PDF puede ser mixto.
    Lanza RuntimeError (con el detalle de cada lector) si ninguno pudo abrir el PDF.
    """
    textos: Optional[list[str]] = None
    errores: list[str] = []
    for nombre, fn in _EXTRACTORES:
        try:
            r = fn(ruta)
        except Exception as e:
            errores.append(f"{nombre}: {type(e).__name__}: {e}")
            continue
        if textos is None or sum(len(t.strip()) for t in r) > sum(len(t.strip()) for t in textos):
            textos = r
        if sum(len(t.strip()) for t in r) >= MIN_CARACTERES_PAGINA:
            break
    if textos is None:
        raise RuntimeError("; ".join(errores) or "no hay ningún lector de PDF instalado")

    # --- OCR de las páginas sin texto
    sin_texto = [i for i, t in enumerate(textos) if len(t.strip()) < MIN_CARACTERES_PAGINA]
    if sin_texto:
        ocr = _preparar_ocr()
        rend = _abrir_renderizador(ruta) if ocr else None
        if ocr and rend:
            render, cerrar = rend
            pendientes = sin_texto[:OCR_MAX_PAGINAS_POR_PDF]
            lote_n = max(1, OCR_HILOS) * 2
            try:
                with ThreadPoolExecutor(max_workers=max(1, OCR_HILOS), thread_name_prefix="ar-ocr") as pool:
                    for k in range(0, len(pendientes), lote_n):
                        imgs = []
                        for i in pendientes[k:k + lote_n]:
                            try:
                                imgs.append((i, render(i)))
                            except Exception:
                                pass
                        futs = [(i, pool.submit(_ocr_imagen, ocr, img)) for i, img in imgs]
                        for i, f in futs:
                            try:
                                t = f.result()
                            except Exception:
                                continue
                            if len(t.strip()) > len(textos[i].strip()):
                                textos[i] = t
                                est["paginas_ocr"] += 1
                        del imgs, futs
            finally:
                try:
                    cerrar()
                except Exception:
                    pass

    for t in textos:
        est["paginas_total"] += 1
        if len(t.strip()) < MIN_CARACTERES_PAGINA:
            est["paginas_sin_texto"] += 1
    return [(i + 1, t) for i, t in enumerate(textos)]


def _tipo_y_titulo(paginas: list[tuple[int, str]], nombre_archivo: str,
                   limite_linea: Optional[int] = None) -> tuple[str, str]:
    """
    ('ACTA'|'RESOLUCIÓN'|'DOCUMENTO', texto normalizado del encabezado).
    El encabezado son las primeras LINEAS_TITULO líneas de la pág. 1, pero SOLO
    las que están antes de `limite_linea` (primera aparición de un aprendiz):
    así las filas de una tabla no cuentan como título.
    """
    lineas = []
    if paginas:
        todas = paginas[0][1].split("\n")
        if limite_linea is not None:
            todas = todas[:limite_linea]
        lineas = [l for l in todas if l.strip()][:LINEAS_TITULO]
    norm = [_normalizar(l) for l in lineas]

    def primera(patron):
        return next((i for i, l in enumerate(norm) if re.search(patron, l)), None)

    i_res, i_act = primera(r"\bRESOLUCION\b"), primera(r"\bACTA\b")
    if i_res is not None or i_act is not None:
        if i_act is None or (i_res is not None and i_res < i_act):
            tipo = "RESOLUCIÓN"
        else:
            tipo = "ACTA"
    else:
        n = _normalizar(nombre_archivo)
        tipo = "RESOLUCIÓN" if "RESOLUCION" in n else "ACTA" if "ACTA" in n else "DOCUMENTO"
    return tipo, " ".join(norm)


def _fragmento(lineas_raw: list[str], ini: int, fin: int, terminos: list[str]) -> str:
    """Texto de la ventana, centrado en el primer término hallado."""
    crudo = re.sub(r"\s+", " ", " ".join(lineas_raw[ini:fin + 1])).strip()
    pos = 0
    if terminos:
        p = _normalizar(crudo).find(terminos[0])
        pos = max(0, p - 80) if p >= 0 else 0
    f = crudo[pos:pos + LARGO_FRAGMENTO].strip()
    return ("…" if pos > 0 else "") + f + ("…" if pos + LARGO_FRAGMENTO < len(crudo) else "")


def _analizar_pdf(ruta: Path, indice: _Indice, est: dict) -> list[Evidencia]:
    paginas = _texto_paginas(ruta, est)
    if not any(t.strip() for _, t in paginas):
        est["pdfs_sin_texto"].append(ruta.name)
        return []

    # --- dónde aparece cada aprendiz en cada página (línea -> {doc: 'DOCUMENTO'|'NOMBRE'})
    datos_pagina = []
    for num_pag, texto in paginas:
        lineas = texto.split("\n")
        if not any(l.strip() for l in lineas):
            continue
        norm = [_normalizar(l) for l in lineas]
        limpio = [_limpiar_digitos(l) for l in lineas]
        hits: dict[int, dict[str, str]] = {}
        for i, (ln, nm) in enumerate(zip(limpio, norm)):
            hallados: dict[str, str] = {}
            for tok in _RE_NUMERO.findall(ln):
                if tok in indice.docs:
                    hallados[tok] = "DOCUMENTO"
            if indice.por_palabra:
                cuenta: Counter = Counter()
                for p in set(_RE_PALABRA.findall(nm)):
                    for d in indice.por_palabra.get(p, ()):
                        cuenta[d] += 1
                for d, c in cuenta.items():
                    if c >= indice.minimo.get(d, 99) and d not in hallados:
                        hallados[d] = "NOMBRE"
            if hallados:
                hits[i] = hallados
        datos_pagina.append((num_pag, lineas, norm, hits))

    primera_pag = datos_pagina[0] if datos_pagina and datos_pagina[0][0] == 1 else None
    limite = min(primera_pag[3]) if primera_pag and primera_pag[3] else None
    tipo, titulo_norm = _tipo_y_titulo(paginas, ruta.name, limite)
    cats_titulo, titulo_condicional = _clasificar(titulo_norm)
    if titulo_condicional:
        cats_titulo = {}

    evidencias: list[Evidencia] = []
    vistas: set[tuple] = set()

    for num_pag, lineas, norm, hits in datos_pagina:
        def otro_en(j: int, doc: str, hits=hits) -> bool:
            return any(d != doc for d in hits.get(j, ()))

        for i, hallados in hits.items():
            for doc, por in hallados.items():
                ini = i
                while ini > i - VENTANA_ANTES and ini - 1 >= 0 and not otro_en(ini - 1, doc):
                    ini -= 1
                fin = i
                while fin < i + VENTANA_DESPUES and fin + 1 < len(lineas) and not otro_en(fin + 1, doc):
                    fin += 1
                ventana = " ".join(norm[ini:fin + 1])

                cats, solo_cond = _clasificar(ventana)
                neg = _RE_NEGACION.search(ventana)
                desde_titulo = False
                if not cats and not solo_cond and cats_titulo:
                    cats, desde_titulo = cats_titulo, True
                    neg = neg or _RE_NEGACION.search(titulo_norm)

                if not cats:
                    if solo_cond:
                        cats = {CANCELACION: [m.group(0) for m in CATEGORIAS[CANCELACION].finditer(ventana)][:3]}
                    else:
                        clave = (doc, num_pag, MENCIONADO, por)
                        if clave not in vistas:
                            vistas.add(clave)
                            evidencias.append(Evidencia(
                                doc, MENCIONADO, ruta.name, num_pag, tipo,
                                _fragmento(lineas, ini, fin, []), por))
                        continue

                for cat, terminos in cats.items():
                    clave = (doc, num_pag, cat, por)
                    if clave in vistas:
                        continue
                    vistas.add(clave)
                    nota = ""
                    revisar = False
                    if neg:
                        revisar = True
                        nota = f"el texto menciona «{neg.group(0).lower()}» (posible levantamiento o revocatoria)"
                    elif solo_cond and cat == CANCELACION:
                        revisar = True
                        nota = "la cancelación aparece solo como advertencia condicional"
                    evidencias.append(Evidencia(
                        doc, cat, ruta.name, num_pag, tipo,
                        _fragmento(lineas, ini, fin, terminos), por,
                        desde_titulo=desde_titulo, revisar=revisar, nota=nota,
                        terminos=", ".join(t.lower() for t in terminos)))
    return evidencias


# ============================================================
# Decisión por aprendiz
# ============================================================

@dataclass
class _Decision:
    estado_real: str
    observacion: str
    grupo: str        # CANCELACION | CONDICIONADO | REVISAR | SIN_NOVEDAD
    cambio: bool      # el estado real difiere del base


def _linea_evidencia(e: Evidencia) -> str:
    pref = "⚠ REVISAR · " if e.revisar else ""
    s = f"{pref}{ETIQUETA[e.categoria]} · {e.tipo_documento} «{e.archivo}», pág. {e.pagina}"
    if e.fragmento:
        s += f": «{e.fragmento}»"
    marcas = []
    if e.encontrado_por == "NOMBRE":
        marcas.append("hallado por nombre, verificar documento")
    if e.desde_titulo:
        marcas.append("según el título del documento")
    if e.nota:
        marcas.append(e.nota)
    if marcas:
        s += " [" + "; ".join(marcas) + "]"
    return s


def _decidir(base: str, evidencias: list[Evidencia], n_pdfs: int) -> _Decision:
    base_n = _normalizar(base)
    usar = [e for e in evidencias if e.encontrado_por == "DOCUMENTO"] or evidencias
    relevantes = [e for e in usar if e.categoria in ESTADO_POR_CATEGORIA]
    firmes = [e for e in relevantes if not e.revisar]
    mencionados = [e for e in usar if e.categoria == MENCIONADO]
    estado = base or "SIN INFORMACIÓN"

    def orden(e):
        return (e.revisar, PRIORIDAD[e.categoria], e.desde_titulo, e.pagina)

    if firmes:
        cat = CANCELACION if any(e.categoria == CANCELACION for e in firmes) else CONDICIONADO
        nuevo = ESTADO_POR_CATEGORIA[cat]
        lineas = []
        grupo = cat
        if base_n and _ESTADO_BASE_TERMINAL.search(base_n) and base_n != _normalizar(nuevo):
            lineas.append(f"⚠ REVISAR · El consolidado ya registra «{base}» y los PDFs indican {nuevo}; "
                          f"se conserva el estado del consolidado.")
            grupo = "REVISAR"
        else:
            estado = nuevo
        ev = sorted(relevantes, key=orden)
        lineas += [_linea_evidencia(e) for e in ev[:MAX_EVIDENCIAS_EN_OBSERVACION]]
        if len(ev) > MAX_EVIDENCIAS_EN_OBSERVACION:
            lineas.append(f"(+{len(ev) - MAX_EVIDENCIAS_EN_OBSERVACION} evidencia(s) más en la hoja «Evidencias PDF»)")
        return _Decision(estado, "\n".join(lineas), grupo, _normalizar(estado) != base_n)

    if relevantes:  # todas marcadas REVISAR
        ev = sorted(relevantes, key=orden)
        lineas = [_linea_evidencia(e) for e in ev[:MAX_EVIDENCIAS_EN_OBSERVACION]]
        if len(ev) > MAX_EVIDENCIAS_EN_OBSERVACION:
            lineas.append(f"(+{len(ev) - MAX_EVIDENCIAS_EN_OBSERVACION} más en la hoja «Evidencias PDF»)")
        return _Decision(estado, "\n".join(lineas), "REVISAR", False)

    if mencionados:
        archivos = sorted({f"{e.archivo} (pág. {e.pagina})" for e in mencionados})
        txt = "Aparece en " + ", ".join(archivos[:3])
        if len(archivos) > 3:
            txt += f" y {len(archivos) - 3} más"
        txt += ", sin términos de condicionamiento ni cancelación."
        if "CONDICION" in base_n or "CANCEL" in base_n:
            txt += f" El consolidado lo registra como «{base}»: sin soporte en esos documentos."
        return _Decision(estado, txt, "SIN_NOVEDAD", False)

    txt = f"No aparece en las actas/resoluciones cargadas ({n_pdfs} PDF revisados)."
    if "CONDICION" in base_n or "CANCEL" in base_n:
        txt += f" El consolidado lo registra como «{base}»: sin soporte en los PDFs."
    return _Decision(estado, txt, "SIN_NOVEDAD", False)


# ============================================================
# Excel de salida
# ============================================================

def _fill_grupo(grupo: str):
    return {CANCELACION: _FILL_CANCELACION, CONDICIONADO: _FILL_CONDICIONADO,
            "REVISAR": _FILL_REVISAR}.get(grupo)


def _escribir_columnas(ws, t: _Tabla, decidir_fila: Callable[[str, str, str], _Decision]) -> None:
    ref_enc = ws.cell(t.fila_enc, t.ultima_col)
    for col, titulo, ancho in ((t.col_estado_real, COL_ESTADO_REAL, 30), (t.col_obs, COL_OBSERVACION, 95)):
        h = ws.cell(t.fila_enc, col)
        h.value = titulo
        if t.nuevas:
            h.font, h.fill = copy(ref_enc.font), copy(ref_enc.fill)
            h.border, h.alignment = copy(ref_enc.border), copy(ref_enc.alignment)
        ws.column_dimensions[get_column_letter(col)].width = ancho

    for fila, doc, base, nombre in t.filas:
        d = decidir_fila(doc, base, nombre)
        ref = ws.cell(fila, t.ultima_col)
        c1, c2 = ws.cell(fila, t.col_estado_real), ws.cell(fila, t.col_obs)
        c1.value, c2.value = d.estado_real, d.observacion
        for c in (c1, c2):
            c.border = copy(ref.border)
            c.font = copy(ref.font)
            c.alignment = Alignment(vertical="top", wrap_text=True)
        relleno = _fill_grupo(d.grupo)
        if relleno:
            c1.fill = relleno

    # Si la hoja tenía autofiltro, se extiende para incluir las columnas nuevas.
    if ws.auto_filter and ws.auto_filter.ref:
        ws.auto_filter.ref = (
            f"A{t.fila_enc}:{get_column_letter(max(t.col_obs, t.col_estado_real))}{ws.max_row}"
        )


def _nombre_hoja_libre(wb, base: str) -> str:
    n, i = base, 2
    while n in wb.sheetnames:
        n, i = f"{base} ({i})", i + 1
    return n[:31]


def _encabezar(ws, titulos: list[str], altura: int = 32) -> None:
    ws.append(titulos)
    for c in ws[1]:
        c.font, c.fill, c.border = _ENC_FONT, _ENC_FILL, _BORDE
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = altura


# ============================================================
# Punto de entrada
# ============================================================

def procesar(
    archivo_consolidado: Path,
    carpeta_actas: Path,
    salida_dir: Path,
    progress_callback: ProgressCallback = None,
) -> dict:
    """
    Lee `archivo_consolidado`, revisa todos los PDFs de `carpeta_actas`
    (recursivo) y escribe una copia del consolidado con las columnas
    ESTADO REAL y OBSERVACIÓN ACTAS/RESOLUCIONES en `salida_dir`.
    Lanza ValueError con un mensaje legible si algo no se puede procesar.
    """
    archivo_consolidado = Path(archivo_consolidado)
    if archivo_consolidado.suffix.lower() not in EXTENSIONES_CONSOLIDADO:
        raise ValueError("El consolidado debe ser un Excel .xlsx o .xlsm.")

    # ---- 1. Consolidado
    _notificar(progress_callback, "Leyendo el consolidado…", 0, 1)
    try:
        wb = load_workbook(archivo_consolidado, keep_vba=archivo_consolidado.suffix.lower() == ".xlsm")
    except Exception as e:
        raise ValueError(f"No se pudo abrir el consolidado ({type(e).__name__}: {e}).")

    # Reproceso: las hojas generadas por una ejecución anterior se regeneran
    # (y no deben tomarse como tablas de aprendices).
    for nombre_hoja in list(wb.sheetnames):
        if re.fullmatch(r"(Resumen Actas|Evidencias PDF)(?: \(\d+\))?", nombre_hoja):
            del wb[nombre_hoja]

    tablas: list[_Tabla] = []
    hojas_omitidas: list[dict] = []
    for ws in wb.worksheets:
        t = _detectar_tabla(ws)
        if t is None:
            hojas_omitidas.append({"hoja": ws.title, "motivo": "no se encontró una columna de documento del aprendiz"})
        elif not t.filas:
            hojas_omitidas.append({"hoja": ws.title, "motivo": "la tabla no tiene filas con documento"})
        else:
            tablas.append(t)
    if not tablas:
        raise ValueError(
            "Ninguna hoja del consolidado tiene una columna de documento "
            "(ej. 'Número de Documento', 'Documento', 'Identificación')."
        )

    nombres_por_doc: dict[str, str] = {}
    for t in tablas:
        for _, doc, _, nombre in t.filas:
            if doc not in nombres_por_doc or (not nombres_por_doc[doc] and nombre):
                nombres_por_doc[doc] = nombre
    indice = _Indice(nombres_por_doc)

    # ---- 2. PDFs
    pdfs = sorted(
        p for p in Path(carpeta_actas).rglob("*")
        if p.is_file() and p.suffix.lower() in EXTENSIONES_PDF and not p.name.startswith("~$")
    )
    if not pdfs:
        raise ValueError("No hay PDFs para revisar en las actas y resoluciones.")

    est = {"paginas_total": 0, "paginas_sin_texto": 0, "paginas_ocr": 0, "pdfs_sin_texto": []}
    evidencias_por_doc: dict[str, list[Evidencia]] = defaultdict(list)
    todas: list[Evidencia] = []
    pdfs_usados: list[str] = []
    pdfs_omitidos: list[dict] = []
    for n, ruta in enumerate(pdfs, start=1):
        _notificar(progress_callback, f"Leyendo PDFs: {n}/{len(pdfs)}…", n, len(pdfs))
        try:
            evs = _analizar_pdf(ruta, indice, est)
        except Exception as e:  # cifrado, corrupto, etc.
            pdfs_omitidos.append({"archivo": ruta.name, "motivo": f"no se pudo leer ({type(e).__name__}: {e})"})
            continue
        pdfs_usados.append(ruta.name)
        for e in evs:
            evidencias_por_doc[e.doc].append(e)
        todas.extend(evs)

    if not pdfs_usados:
        motivos = " | ".join(f"{o['archivo']} -> {o['motivo']}" for o in pdfs_omitidos[:3])
        raise ValueError(
            f"No se pudo leer ninguno de los PDFs de la Base de Datos ({len(pdfs_omitidos)} fallaron). "
            f"Detalle: {motivos}"
        )
    if est["paginas_total"] and est["paginas_sin_texto"] == est["paginas_total"]:
        ocr_ok = bool(_preparar_ocr())
        raise ValueError(
            "Ninguna página de los PDFs tiene texto legible. "
            + ("El OCR está activo pero no logró leer las páginas (revisa calidad del escaneo)."
               if ocr_ok else
               "Parecen escaneados y el OCR no está disponible: instala el binario tesseract "
               "(tesseract-ocr y tesseract-ocr-spa) y los paquetes pytesseract y pypdfium2 (o pymupdf).")
        )

    # ---- 3. Decisión y escritura
    _notificar(progress_callback, "Generando el Excel…", 0, 1)
    n_pdfs = len(pdfs_usados)
    cache: dict[tuple[str, str], _Decision] = {}

    def decidir(doc: str, base: str, _nombre: str) -> _Decision:
        k = (doc, base)
        if k not in cache:
            cache[k] = _decidir(base, evidencias_por_doc.get(doc, []), n_pdfs)
        return cache[k]

    resumen_por_aprendiz: dict[tuple[str, str], tuple[_Tabla, str, str, _Decision]] = {}
    for t in tablas:
        ws = wb[t.hoja]
        _escribir_columnas(ws, t, decidir)
        for _, doc, base, nombre in t.filas:
            resumen_por_aprendiz.setdefault((t.hoja, doc), (t, base, nombre, decidir(doc, base, nombre)))

    grupos = Counter(d.grupo for *_, d in resumen_por_aprendiz.values())
    cambios = sum(1 for *_, d in resumen_por_aprendiz.values() if d.cambio)
    mencionados = sum(
        1 for (_, doc), (_, _, _, d) in resumen_por_aprendiz.items()
        if d.grupo == "SIN_NOVEDAD" and any(e.categoria == MENCIONADO for e in evidencias_por_doc.get(doc, []))
    )
    docs_unicos = {doc for (_, doc) in resumen_por_aprendiz}

    resultado = {
        "archivo_generado": "",
        "total_aprendices": len(docs_unicos),
        "total_filas_aprendiz_hoja": len(resumen_por_aprendiz),
        "total_en_resolucion_cancelacion": int(grupos.get(CANCELACION, 0)),
        "total_condicionados": int(grupos.get(CONDICIONADO, 0)),
        "total_por_revisar": int(grupos.get("REVISAR", 0)),
        "total_sin_novedad": int(grupos.get("SIN_NOVEDAD", 0)),
        "total_mencionados_sin_novedad": int(mencionados),
        "total_con_cambio_de_estado": int(cambios),
        "hojas_procesadas": [t.hoja for t in tablas],
        "hojas_omitidas": hojas_omitidas,
        "pdfs_usados": pdfs_usados,
        "pdfs_omitidos": pdfs_omitidos,
        "pdfs_sin_texto": est["pdfs_sin_texto"],
        "paginas_total": est["paginas_total"],
        "paginas_sin_texto": est["paginas_sin_texto"],
        "paginas_ocr": est["paginas_ocr"],
    }

    # ---- Hoja de resumen
    ws_r = wb.create_sheet(_nombre_hoja_libre(wb, "Resumen Actas"))
    filas = [
        ("ESTADO REAL SEGÚN ACTAS Y RESOLUCIONES",),
        ("Consolidado", archivo_consolidado.name),
        ("PDFs revisados", len(pdfs_usados)),
        ("Páginas revisadas", est["paginas_total"]),
        ("",),
        ("Resultado (aprendices por hoja)",),
        ("Aprendices distintos", resultado["total_aprendices"]),
        ("EN RESOLUCIÓN DE CANCELACIÓN", resultado["total_en_resolucion_cancelacion"]),
        ("CONDICIONADO", resultado["total_condicionados"]),
        ("Por revisar manualmente", resultado["total_por_revisar"]),
        ("Sin novedad en los PDFs", resultado["total_sin_novedad"]),
        ("Estado real distinto al del consolidado", resultado["total_con_cambio_de_estado"]),
        ("",),
        ("Cómo se decide",),
        ("Búsqueda", "Por número de documento (con o sin puntos); respaldo por nombre (≥3 palabras en la misma línea)."),
        ("Ventana", f"{VENTANA_ANTES} líneas antes y {VENTANA_DESPUES} después, cortada donde empieza otro aprendiz."),
        ("CANCELACIÓN", "Términos: cancelación / cancelar / cancelese…  → EN RESOLUCIÓN DE CANCELACIÓN"),
        ("CONDICIONADO", "Términos: condicionado(a) / condicionamiento / condicionar…  → CONDICIONADO"),
        ("REVISAR", "Hay lenguaje de levantamiento/revocatoria, cancelación solo como advertencia, o choque con el estado del consolidado."),
    ]
    if hojas_omitidas:
        filas += [("",), ("Hojas omitidas",)] + [(o["hoja"], o["motivo"]) for o in hojas_omitidas]
    if pdfs_omitidos:
        filas += [("",), ("PDFs omitidos",)] + [(o["archivo"], o["motivo"]) for o in pdfs_omitidos]
    if est["pdfs_sin_texto"]:
        filas += [("",), ("PDFs sin texto (¿escaneados?, no se pudieron revisar)",)] + [(n,) for n in est["pdfs_sin_texto"]]
    if est["paginas_sin_texto"] and not est["pdfs_sin_texto"]:
        filas += [("",), ("Páginas sin texto (no revisadas)", est["paginas_sin_texto"])]
    if est["paginas_ocr"]:
        filas += [("",), ("Páginas leídas con OCR (verificar)", est["paginas_ocr"])]
    for f in filas:
        ws_r.append(list(f))
    for c in ws_r["A"]:
        if c.value and ws_r.cell(row=c.row, column=2).value is None:
            c.font = Font(bold=True, size=12)
    ws_r.column_dimensions["A"].width = 46
    ws_r.column_dimensions["B"].width = 90

    # ---- Hoja de evidencias
    ws_e = wb.create_sheet(_nombre_hoja_libre(wb, "Evidencias PDF"))
    _encabezar(ws_e, ["DOCUMENTO", "NOMBRE", "HOJA (FICHA)", "ESTADO BASE", "ESTADO REAL", "CATEGORÍA",
                      "TIPO", "ARCHIVO", "PÁGINA", "HALLADO POR", "REVISAR", "NOTA", "FRAGMENTO"])
    info_doc: dict[str, tuple[str, str, str, str]] = {}
    for (hoja, doc), (_, base, nombre, d) in resumen_por_aprendiz.items():
        info_doc.setdefault(doc, (hoja, base, nombre, d.estado_real))
    for e in sorted(todas, key=lambda x: (x.doc, PRIORIDAD[x.categoria], x.archivo, x.pagina)):
        hoja, base, nombre, estado_real = info_doc.get(e.doc, ("", "", "", ""))
        ws_e.append([e.doc, nombre, hoja, base, estado_real, ETIQUETA[e.categoria], e.tipo_documento,
                     e.archivo, e.pagina, e.encontrado_por, "SÍ" if e.revisar else "",
                     e.nota + ("; según el título" if e.desde_titulo else ""), e.fragmento])
        for c in ws_e[ws_e.max_row]:
            c.border = _BORDE
        ws_e.cell(row=ws_e.max_row, column=1).number_format = "@"
    for i, a in enumerate([15, 32, 14, 18, 28, 16, 13, 38, 9, 13, 9, 40, 90], start=1):
        ws_e.column_dimensions[get_column_letter(i)].width = a
    ws_e.freeze_panes = "B2"
    ws_e.auto_filter.ref = f"A1:{get_column_letter(13)}{max(ws_e.max_row, 2)}"

    ext = ".xlsm" if archivo_consolidado.suffix.lower() == ".xlsm" else ".xlsx"
    ruta_salida = Path(salida_dir) / f"{archivo_consolidado.stem}_Estado_Real{ext}"
    ruta_salida.parent.mkdir(parents=True, exist_ok=True)
    wb.save(ruta_salida)

    resultado["archivo_generado"] = str(ruta_salida)
    _notificar(progress_callback, "Listo.", 1, 1)
    return resultado