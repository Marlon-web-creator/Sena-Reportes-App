"""
modules/no_programados.py

Lógica de NoProgramados.py adaptada para la API web.

Optimizaciones aplicadas en esta versión:
- LRU real: los Futures se liberan al mover el resultado a caché (evita OOM).
- rapidfuzz en lugar de difflib (10-30× más rápido en fase de análisis).
- OCR diferido: el indexado NO hace OCR salvo que se pida por env var.
- Indexado por nombre de archivo antes que por contenido.
- Caché en disco del texto extraído de cada PDF (gzip JSON en /tmp).
- Caché en disco del mapa ficha->PDF (por hash del inventario).

Fixes anti-bucle aplicados:
- La caché registra si el OCR fue *intentado*, no si tuvo éxito.
  Un PDF escaneado que agota el timeout se marca como "ya intentado"
  y no se reintenta en cada ficha / análisis posterior.
- La caché en disco se escribe SIEMPRE, también cuando hay error, para
  que un PDF problemático no se vuelva a procesar en la siguiente
  ejecución del mismo contenedor.
- La clave de la caché en memoria es (Path, permitir_ocr); una versión
  "con OCR" puede servir a un caller que pide "sin OCR".
"""

import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FuturesTimeout,
)
from copy import copy
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF
import openpyxl
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
# CONFIGURACIÓN
# ============================================================

COL_CODIGO = 1
COL_COMPETENCIA = 8
FILA_INICIO_DATOS = 5

COL_ESTADO = 11
COL_OBSERVACION = 12
COL_ARCHIVOS = 13
COL_DETALLE = 14
COL_EVIDENCIA = 15
COL_DOCUMENTO = 4

IDIOMA_OCR = "spa"
DPI_OCR = int(os.environ.get("NP_OCR_DPI") or 150)
MIN_CARACTERES_TEXTO_PAGINA = 20

# ------------------------------------------------------------
# VERIFICACIÓN DE MARCADOR "INSTRUCTOR PENDIENTE / SIN ASIGNAR"
# ------------------------------------------------------------
# Palabras que, si aparecen en la MISMA FILA de tabla donde matcheó la
# competencia, invalidan esa coincidencia como "programado" (el evento
# existe en el PDF pero no tiene instructor asignado). El set normalizado
# (tildes/mayúsculas) se arma más abajo, justo después de normalizar_texto().
_PALABRAS_MARCADOR_PENDIENTE_RAW = ("PENDIENTE", "INSTRUCTOR")

# DPI para el recorte + re-OCR de verificación (Paso 2). Solo se aplica
# a la franja de la fila donde ya hubo match, no a la página completa.
DPI_OCR_VERIFICACION = int(os.environ.get("NP_OCR_DPI_VERIFICACION") or 300)

# Margen (en puntos PDF, 1pt = 1/72") que se agrega arriba/abajo de la
# fila detectada antes de recortar, para no cortar texto por el borde.
MARGEN_VERIFICACION_PT = float(os.environ.get("NP_MARGEN_VERIFICACION_PT") or 8.0)

# Tolerancia (en puntos PDF) para agrupar palabras en la misma fila de
# tabla, en páginas con texto digital (no escaneadas).
TOLERANCIA_FILA_PT = float(os.environ.get("NP_TOLERANCIA_FILA_PT") or 3.0)

# Umbral de similitud difusa para el reconocimiento de las palabras del
# marcador, para tolerar ruido de OCR incluso a 300 DPI.
UMBRAL_MARCADOR_PENDIENTE = float(os.environ.get("NP_UMBRAL_MARCADOR") or 0.87)

# v2: cada página ahora también guarda "filas" (texto agrupado por
# coordenada Y). Se sube la versión para invalidar automáticamente la
# caché en disco generada por versiones anteriores del script.
CACHE_FORMATO_VERSION = 2

UMBRAL_SIMILITUD_VENTANA = 0.82
UMBRAL_COBERTURA = 0.80
UMBRAL_PALABRA = 0.88
TAMANO_VENTANA_PALABRAS = 80

MIN_DIGITOS_FICHA = 5

COMPETENCIAS_FORZADAS_PROGRAMADO = {
    "RESULTADOS DE APRENDIZAJE ETAPA PRACTICA",
}

PALABRAS_VACIAS = {
    "DE", "DEL", "LA", "LAS", "EL", "LOS", "EN", "Y", "O",
    "A", "AL", "UN", "UNA", "POR", "PARA", "CON", "QUE",
    "SE", "SU", "SUS", "ES", "SON", "UNO",
}

ProgressCallback = Optional[Callable[[str, int, int], None]]

# ------------------------------------------------------------
# PARALELISMO (ajustable por variables de entorno)
# ------------------------------------------------------------
_CPU = os.cpu_count() or 4

NUM_WORKERS_PDF = int(os.environ.get("NP_WORKERS_PDF") or min(6, _CPU * 2))
NUM_WORKERS_FICHAS = int(os.environ.get("NP_WORKERS_FICHAS") or max(2, min(4, _CPU)))
MAX_OCR_CONCURRENTES = int(os.environ.get("NP_MAX_OCR") or 1)
PDF_CACHE_MAX_SIZE = int(os.environ.get("NP_CACHE_MAX") or 200)
SKIP_OCR = os.environ.get("NP_SKIP_OCR", "").lower() in ("1", "true", "yes")

# >>> Por defecto NO se hace OCR durante el indexado.
#     Actívalo con NP_OCR_INDEXADO=1 si tus PDFs escaneados no traen
#     el número de ficha en el nombre del archivo.
OCR_EN_INDEXADO = os.environ.get("NP_OCR_INDEXADO", "0").lower() in ("1", "true", "yes")

TIMEOUT_PDF_PRELOAD = float(os.environ.get("NP_TIMEOUT_PDF") or 60)
TIMEOUT_OCR_POR_PAGINA = float(os.environ.get("NP_TIMEOUT_OCR") or 20)

# Carpeta para la caché en disco (sobrevive entre ejecuciones del mismo contenedor).
CACHE_DISCO_DIR = Path(os.environ.get("NP_TEXTO_CACHE") or "/tmp/np_texto")
try:
    CACHE_DISCO_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    logger.exception("No se pudo crear %s; caché en disco deshabilitada", CACHE_DISCO_DIR)
    CACHE_DISCO_DIR = None

_OCR_SEMAPHORE = threading.Semaphore(MAX_OCR_CONCURRENTES)

try:
    if SKIP_OCR:
        raise RuntimeError("OCR deshabilitado por NP_SKIP_OCR")
    import pytesseract
    from PIL import Image
    pytesseract.get_tesseract_version()
    OCR_DISPONIBLE = True
except Exception as _e:
    OCR_DISPONIBLE = False
    logger.warning("OCR no disponible: %s", _e)

logger.info(
    "Config: workers_pdf=%d workers_fichas=%d max_ocr=%d cache_max=%s "
    "ocr=%s ocr_indexado=%s dpi=%d dpi_verificacion=%d timeout_pdf=%.0fs "
    "timeout_ocr=%.0fs disco=%s cache_v=%d",
    NUM_WORKERS_PDF, NUM_WORKERS_FICHAS, MAX_OCR_CONCURRENTES,
    PDF_CACHE_MAX_SIZE or "ilimitado", OCR_DISPONIBLE, OCR_EN_INDEXADO, DPI_OCR,
    DPI_OCR_VERIFICACION, TIMEOUT_PDF_PRELOAD, TIMEOUT_OCR_POR_PAGINA,
    CACHE_DISCO_DIR, CACHE_FORMATO_VERSION,
)


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


PALABRAS_MARCADOR_PENDIENTE = {
    normalizar_texto(w) for w in _PALABRAS_MARCADOR_PENDIENTE_RAW
}


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


def palabras_importantes(texto):
    return [p for p in normalizar_texto(texto).split()
            if p not in PALABRAS_VACIAS and len(p) >= 3]


def similitud_palabra(a, b):
    # rapidfuzz devuelve 0-100 → normalizamos a 0-1.
    return fuzz.ratio(a, b) / 100.0


def fila_es_valida(ws, fila):
    valor_codigo = ws.cell(fila, COL_CODIGO).value
    valor_competencia = ws.cell(fila, COL_COMPETENCIA).value
    if valor_codigo is None or valor_competencia is None:
        return False
    texto_codigo = str(valor_codigo).strip().upper()
    if not texto_codigo or "TOTAL" in texto_codigo or texto_codigo == "CODIGO":
        return False
    return bool(str(valor_competencia).strip())


# ============================================================
# CACHÉ EN DISCO DEL TEXTO EXTRAÍDO
# ============================================================

def _cache_file_pdf(pdf_path: Path) -> Optional[Path]:
    if CACHE_DISCO_DIR is None:
        return None
    try:
        st = pdf_path.stat()
    except OSError:
        return None
    h = hashlib.md5(
        f"{pdf_path.name}:{st.st_size}:{int(st.st_mtime)}:v{CACHE_FORMATO_VERSION}".encode()
    ).hexdigest()
    return CACHE_DISCO_DIR / f"txt_{h}.json.gz"


def _leer_cache_texto(cache_file: Path):
    try:
        with gzip.open(cache_file, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        logger.exception("Error leyendo cache %s", cache_file)
        return None


def _escribir_cache_texto(cache_file: Path, data: dict):
    try:
        with gzip.open(cache_file, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        logger.exception("Error escribiendo cache %s", cache_file)


# ============================================================
# CACHÉ DE PDFs — LRU real + Futures liberados + OCR diferido
# ============================================================

class PDFCache:
    """
    Caché LRU de PDFs cargados. Thread-safe.

    - La clave es (Path, permitir_ocr). Así una versión "sin OCR" y otra
      "con OCR" del mismo PDF conviven sin pisarse ni relanzarse.
    - Si solo tenemos la versión "con OCR" y piden "sin OCR", se sirve
      igual (es superset).
    - Los Futures se liberan tan pronto como el resultado pasa a caché.
    - `max_size` acota cuántas entradas (path, permitir_ocr) caben en RAM.
    """

    def __init__(self, max_workers: int = NUM_WORKERS_PDF,
                 max_size: int = PDF_CACHE_MAX_SIZE):
        # clave: tuple[Path, bool] → {"paginas": [...], "error": str|None}
        self._cache: "OrderedDict[tuple, dict]" = OrderedDict()
        self._futures: dict[tuple, Future] = {}
        self._lock = threading.Lock()
        self._max_size = max_size if max_size and max_size > 0 else None
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="np-pdf"
        )

    def get(self, pdf_path: Path, permitir_ocr: bool = True):
        clave = (pdf_path, permitir_ocr)
        with self._lock:
            entry = self._cache.get(clave)
            if entry is not None:
                self._cache.move_to_end(clave)
                return entry["paginas"], entry["error"]

            # Si piden "sin OCR" pero solo tenemos "con OCR", la servimos.
            if not permitir_ocr:
                alt = self._cache.get((pdf_path, True))
                if alt is not None:
                    self._cache.move_to_end((pdf_path, True))
                    return alt["paginas"], alt["error"]

            fut = self._futures.get(clave)
            if fut is None:
                fut = self._executor.submit(cargar_pdf, pdf_path, permitir_ocr)
                self._futures[clave] = fut

        # Esperar fuera del lock.
        paginas, error, _ = fut.result()

        with self._lock:
            if self._futures.get(clave) is fut:
                del self._futures[clave]
            self._cache[clave] = {"paginas": paginas, "error": error}
            self._cache.move_to_end(clave)
            self._evictar_locked()
            return self._cache[clave]["paginas"], self._cache[clave]["error"]

    def _evictar_locked(self):
        if not self._max_size:
            return
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def preload(self, pdf_paths, progress_callback: ProgressCallback = None,
                permitir_ocr: bool = False) -> None:
        """
        Dispara la carga de varios PDFs en paralelo y espera a que terminen.
        Con timeout por PDF: si uno se atasca, se marca como error y se sigue.
        Reporta progreso cada 10 PDFs.
        """
        pendientes = []
        with self._lock:
            for p in pdf_paths:
                clave = (p, permitir_ocr)
                if clave in self._cache:
                    continue
                # Si piden "sin OCR" y ya tenemos "con OCR", no hace falta.
                if not permitir_ocr and (p, True) in self._cache:
                    continue
                fut = self._futures.get(clave)
                if fut is None:
                    fut = self._executor.submit(cargar_pdf, p, permitir_ocr)
                    self._futures[clave] = fut
                pendientes.append((p, clave, fut))

        total = len(pendientes)
        if total == 0:
            logger.info("Preload: todo ya estaba en caché (%d PDFs)", len(pdf_paths))
            return

        logger.info("Preload: %d PDFs pendientes de cargar (ocr=%s)",
                    total, permitir_ocr)
        fut_a_info = {fut: (p, clave) for p, clave, fut in pendientes}
        completadas = 0
        errores = 0
        inicio = time.time()

        for fut in as_completed(fut_a_info.keys()):
            p, clave = fut_a_info[fut]
            try:
                resultado = fut.result(timeout=TIMEOUT_PDF_PRELOAD)
                paginas, error, _ = resultado
                if not paginas:
                    errores += 1
                    logger.warning("PDF sin contenido: %s (%s)", p.name, error)
            except FuturesTimeout:
                logger.error(
                    "TIMEOUT: PDF %s excedió %.0fs — se omite",
                    p.name, TIMEOUT_PDF_PRELOAD,
                )
                resultado = ([], f"Timeout de {TIMEOUT_PDF_PRELOAD:.0f}s", False)
                errores += 1
            except Exception as e:
                resultado = ([], f"Error cargando PDF: {e}", False)
                errores += 1
                logger.exception("Error cargando %s", p)

            paginas, error, _ = resultado
            with self._lock:
                if self._futures.get(clave) is fut:
                    del self._futures[clave]
                self._cache[clave] = {"paginas": paginas, "error": error}
                self._cache.move_to_end(clave)
                self._evictar_locked()

            completadas += 1
            if completadas % 10 == 0 or completadas == total:
                seg = time.time() - inicio
                v = completadas / seg if seg > 0 else 0
                logger.info(
                    "Preload: %d/%d PDFs (%.1fs, %.1f PDF/s, %d errores)",
                    completadas, total, seg, v, errores,
                )
                _notificar(
                    progress_callback,
                    f"Leyendo PDFs: {completadas}/{total}…",
                    completadas, total,
                )

        logger.info("Preload: completo en %.1fs", time.time() - inicio)

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)


# ============================================================
# LECTURA DE PDF — texto y OCR, decidido POR PÁGINA
# ============================================================

# ------------------------------------------------------------
# DETECCIÓN DE COLUMNAS DE LA TABLA
# ------------------------------------------------------------
# Agrupar TODAS las palabras de la página solo por coordenada Y (sin
# distinguir columnas) mezcla texto de "Resultado de aprendizaje" con
# el de "Instructor" cuando una celda envuelve en más líneas que su
# vecina — que es exactamente lo que pasa en las plantillas reales
# (una fila de horario puede envolver en 4 líneas de "Resultado de
# aprendizaje" mientras "Instructor" solo ocupa 2-3, y ambas columnas
# comparten alturas Y intermedias). Por eso primero se ubican los
# límites horizontales de las columnas que importan —usando la fila de
# encabezado como referencia— y luego cada columna se procesa por
# separado.

_ENCABEZADOS_COLUMNA = {"RESULTADO", "INSTRUCTOR", "AMBIENTE"}

# Distancia horizontal (puntos PDF) a partir de la cual dos palabras del
# encabezado se consideran de columnas DISTINTAS en vez de la misma
# etiqueta (p. ej. "RESULTADO" y "DE" y "APRENDIZAJE" van juntas; el
# salto hacia "AMBIENTE" es mucho mayor).
BRECHA_COLUMNA_PT = float(os.environ.get("NP_BRECHA_COLUMNA_PT") or 15.0)


def _agrupar_palabras_en_grupos_x(palabras_fila, brecha_min: float):
    """
    Agrupa las palabras de UNA fila (ordenadas por x0) en "etiquetas de
    columna": una brecha horizontal mayor a `brecha_min` entre el final
    de una palabra y el inicio de la siguiente marca el salto a la
    columna vecina.

    palabras_fila: lista de tuplas (x0, y0, x1, y1, texto_normalizado),
    YA ordenada por x0.
    Devuelve una lista ordenada de {"x0", "x1", "texto"}.
    """
    grupos_crudos = []
    actual = []
    for p in palabras_fila:
        if actual and (p[0] - actual[-1][2]) > brecha_min:
            grupos_crudos.append(actual)
            actual = []
        actual.append(p)
    if actual:
        grupos_crudos.append(actual)

    return [
        {
            "x0": min(p[0] for p in g),
            "x1": max(p[2] for p in g),
            "texto": " ".join(p[4] for p in g),
        }
        for g in grupos_crudos
    ]


def _detectar_limites_columnas(candidatos, tolerancia_y: float, brecha_columna: float):
    """
    candidatos: lista de tuplas (x0, y0, x1, y1, texto_normalizado) de
    TODAS las palabras de la página, en cualquier sistema de
    coordenadas (puntos PDF o píxeles) — tolerancia_y y brecha_columna
    deben estar en esas mismas unidades.

    Ubica la fila de encabezado (la que trae, a una altura similar,
    tanto "RESULTADO" como "INSTRUCTOR"), agrupa TODAS sus palabras en
    etiquetas de columna por proximidad horizontal, y calcula los
    límites de "Resultado de aprendizaje" e "Instructor" como el PUNTO
    MEDIO con sus columnas vecinas — mucho más robusto que usar la
    posición cruda de la palabra clave, que puede estar centrada sobre
    un ancho distinto al de los datos de la fila debajo.

    Devuelve (x0_resultado, x1_resultado, x0_instructor, y_header) o
    None si no se pudo determinar con confianza (p. ej. página de
    continuación sin encabezado, u otro formato de tabla).
    """
    marcadores = [p for p in candidatos if p[4] in _ENCABEZADOS_COLUMNA]
    if not marcadores:
        return None

    por_y = {}
    for p in marcadores:
        clave = round(p[1] / tolerancia_y) if tolerancia_y else round(p[1])
        por_y.setdefault(clave, []).append(p)

    y_header = None
    for grupo in por_y.values():
        textos = {p[4] for p in grupo}
        if "RESULTADO" in textos and "INSTRUCTOR" in textos:
            y_header = min(p[1] for p in grupo)
            break
    if y_header is None:
        return None

    # Traer TODAS las palabras de esa fila de encabezado (no solo las
    # de nuestro set de palabras clave), para poder ubicar también las
    # columnas vecinas y calcular límites por punto medio.
    fila_completa = sorted(
        (p for p in candidatos if abs(p[1] - y_header) <= tolerancia_y),
        key=lambda p: p[0],
    )
    if not fila_completa:
        return None

    grupos = _agrupar_palabras_en_grupos_x(fila_completa, brecha_columna)
    idx_resultado = next(
        (i for i, g in enumerate(grupos) if "RESULTADO" in g["texto"].split()), None
    )
    idx_instructor = next(
        (i for i, g in enumerate(grupos) if "INSTRUCTOR" in g["texto"].split()), None
    )
    if idx_resultado is None or idx_instructor is None or idx_instructor <= idx_resultado:
        return None

    # Límite izquierdo de "Resultado": punto medio con la columna
    # anterior (o su propio borde si es la primera columna de la fila).
    if idx_resultado > 0:
        x0_resultado = (grupos[idx_resultado - 1]["x1"] + grupos[idx_resultado]["x0"]) / 2
    else:
        x0_resultado = grupos[idx_resultado]["x0"]

    # Límite derecho de "Resultado" = punto medio con la columna
    # siguiente (normalmente "Ambiente"; si por algún motivo Instructor
    # viene justo después, el punto medio se calcula con esa).
    x1_resultado = (grupos[idx_resultado]["x1"] + grupos[idx_resultado + 1]["x0"]) / 2

    # Límite izquierdo de "Instructor" = punto medio con su columna
    # anterior (típicamente "Ambiente").
    x0_instructor = (grupos[idx_instructor - 1]["x1"] + grupos[idx_instructor]["x0"]) / 2

    return x0_resultado, x1_resultado, x0_instructor, y_header


def _agrupar_columna_en_filas(palabras_columna, tolerancia_y: float):
    """
    Agrupa por coordenada Y las palabras YA FILTRADAS a una sola
    columna (formato PyMuPDF: tuplas x0,y0,x1,y1,texto,...). Al estar
    restringido a una sola columna, esto sí reconstruye correctamente
    el texto de una celda que envuelve en varias líneas, sin mezclarse
    con columnas vecinas.
    """
    if not palabras_columna:
        return []
    palabras_columna = sorted(palabras_columna, key=lambda w: (w[1], w[0]))

    crudas, actual, y_ref = [], [], None
    for w in palabras_columna:
        y0 = w[1]
        if y_ref is None or (y0 - y_ref) <= tolerancia_y:
            actual.append(w)
            if y_ref is None:
                y_ref = y0
        else:
            crudas.append(actual)
            actual = [w]
            y_ref = y0
    if actual:
        crudas.append(actual)

    filas = []
    for grupo in crudas:
        grupo_ordenado = sorted(grupo, key=lambda w: w[0])
        texto = " ".join(w[4] for w in grupo_ordenado)
        filas.append({
            "y0": min(w[1] for w in grupo_ordenado),
            "y1": max(w[3] for w in grupo_ordenado),
            "texto_original": texto,
            "texto_normalizado": normalizar_texto(texto),
        })
    return filas


def _extraer_estructura_columnas_digital(pagina, tolerancia_y: float = TOLERANCIA_FILA_PT):
    """
    Para una página con texto digital: ubica las columnas "Resultado
    de aprendizaje" (donde vive la frase de la competencia a verificar)
    e "Instructor" (donde vive el marcador PENDIENTE/INSTRUCTOR), y
    devuelve el texto de cada una reconstruido por filas, de arriba
    hacia abajo, ya limpio de contaminación entre columnas.

    Devuelve {"resultado": [...], "instructor": [...], "x0_instructor": float}
    o None si no se pudo ubicar el encabezado de la tabla en esta página.
    """
    try:
        palabras = pagina.get_text("words")  # (x0,y0,x1,y1,texto,block,line,word_no)
    except Exception:
        logger.exception("No se pudo extraer 'words' de la página")
        return None
    if not palabras:
        return None

    candidatos = [(w[0], w[1], w[2], w[3], normalizar_texto(w[4])) for w in palabras]
    limites = _detectar_limites_columnas(candidatos, tolerancia_y, BRECHA_COLUMNA_PT)
    if limites is None:
        return None
    x0_res, x1_res, x0_ins, y_header = limites

    palabras_resultado, palabras_instructor = [], []
    for w in palabras:
        x0, y0 = w[0], w[1]
        if y0 <= y_header + tolerancia_y:
            continue  # es parte de la fila de encabezado, no de los datos
        if x0_res <= x0 < x1_res:
            palabras_resultado.append(w)
        elif x0 >= x1_res:
            # Todo lo que queda a la derecha de "Resultado" (Ambiente +
            # Instructor combinados). Se combinan a propósito: cuando
            # una fila no tiene instructor asignado, el texto
            # "PENDIENTE PARA PROGRAMAR..." suele quedar centrado sobre
            # la celda fusionada Ambiente+Instructor, y una palabra
            # como "PENDIENTE" puede caer del lado de "Ambiente" — que
            # se perdería si solo mirásemos la columna "Instructor"
            # en sentido estricto.
            palabras_instructor.append(w)

    return {
        "resultado": _agrupar_columna_en_filas(palabras_resultado, tolerancia_y),
        "instructor": _agrupar_columna_en_filas(palabras_instructor, tolerancia_y),
        "x0_instructor": x1_res,
    }


def _agrupar_filas_ocr(datos_ocr: dict, dpi: float) -> list:
    """
    Agrupa la salida de pytesseract.image_to_data (Output.DICT) en
    "filas", usando el agrupamiento por línea que ya hace Tesseract
    (block_num/par_num/line_num), y convierte las coordenadas de
    píxeles de vuelta a puntos PDF (para poder recortar luego sobre la
    página original a cualquier DPI).
    """
    n = len(datos_ocr.get("text", []))
    if n == 0:
        return []

    escala = dpi / 72.0
    filas_dict = OrderedDict()
    for i in range(n):
        texto = (datos_ocr["text"][i] or "").strip()
        if not texto:
            continue
        clave = (datos_ocr["block_num"][i], datos_ocr["par_num"][i], datos_ocr["line_num"][i])
        filas_dict.setdefault(clave, []).append({
            "x": datos_ocr["left"][i],
            "y": datos_ocr["top"][i],
            "w": datos_ocr["width"][i],
            "h": datos_ocr["height"][i],
            "texto": texto,
        })

    filas = []
    for palabras in filas_dict.values():
        palabras_ordenadas = sorted(palabras, key=lambda p: p["x"])
        texto = " ".join(p["texto"] for p in palabras_ordenadas)
        y0_px = min(p["y"] for p in palabras_ordenadas)
        y1_px = max(p["y"] + p["h"] for p in palabras_ordenadas)
        filas.append({
            "y0": y0_px / escala,
            "y1": y1_px / escala,
            "texto_original": texto,
            "texto_normalizado": normalizar_texto(texto),
        })
    filas.sort(key=lambda f: f["y0"])
    return filas


def _agrupar_columna_ocr_en_filas(palabras_columna, tolerancia_y_px: float, escala: float):
    """
    Igual que _agrupar_columna_en_filas, pero para palabras de OCR
    (dicts con x,y,w,h,texto en píxeles). Convierte las coordenadas de
    vuelta a puntos PDF al final, para que el resto del pipeline
    (recorte a alta resolución, etc.) trabaje siempre en puntos.
    """
    if not palabras_columna:
        return []
    palabras_columna = sorted(palabras_columna, key=lambda p: (p["y"], p["x"]))

    crudas, actual, y_ref = [], [], None
    for p in palabras_columna:
        if y_ref is None or (p["y"] - y_ref) <= tolerancia_y_px:
            actual.append(p)
            if y_ref is None:
                y_ref = p["y"]
        else:
            crudas.append(actual)
            actual = [p]
            y_ref = p["y"]
    if actual:
        crudas.append(actual)

    filas = []
    for grupo in crudas:
        grupo_ordenado = sorted(grupo, key=lambda p: p["x"])
        texto = " ".join(p["texto"] for p in grupo_ordenado)
        y0_px = min(p["y"] for p in grupo_ordenado)
        y1_px = max(p["y"] + p["h"] for p in grupo_ordenado)
        filas.append({
            "y0": y0_px / escala,
            "y1": y1_px / escala,
            "texto_original": texto,
            "texto_normalizado": normalizar_texto(texto),
        })
    return filas


def _extraer_estructura_columnas_ocr(datos_ocr: dict, dpi: float):
    """
    Equivalente a _extraer_estructura_columnas_digital, pero a partir
    de la salida de pytesseract.image_to_data de una página escaneada.
    No hace OCR adicional: reutiliza los mismos datos ya obtenidos.
    """
    n = len(datos_ocr.get("text", []))
    if n == 0:
        return None

    escala = dpi / 72.0
    tolerancia_y_px = TOLERANCIA_FILA_PT * escala

    palabras = []
    for i in range(n):
        texto = (datos_ocr["text"][i] or "").strip()
        if not texto:
            continue
        palabras.append({
            "x": datos_ocr["left"][i], "y": datos_ocr["top"][i],
            "w": datos_ocr["width"][i], "h": datos_ocr["height"][i],
            "texto": texto,
        })
    if not palabras:
        return None

    candidatos = [
        (p["x"], p["y"], p["x"] + p["w"], p["y"] + p["h"], normalizar_texto(p["texto"]))
        for p in palabras
    ]
    brecha_columna_px = BRECHA_COLUMNA_PT * escala
    limites = _detectar_limites_columnas(candidatos, tolerancia_y_px, brecha_columna_px)
    if limites is None:
        return None
    x0_res, x1_res, x0_ins, y_header = limites

    pal_resultado, pal_instructor = [], []
    for p in palabras:
        if p["y"] <= y_header + tolerancia_y_px:
            continue
        if x0_res <= p["x"] < x1_res:
            pal_resultado.append(p)
        elif p["x"] >= x1_res:
            # Ver comentario equivalente en _extraer_estructura_columnas_digital:
            # se combina Ambiente+Instructor a propósito.
            pal_instructor.append(p)

    return {
        "resultado": _agrupar_columna_ocr_en_filas(pal_resultado, tolerancia_y_px, escala),
        "instructor": _agrupar_columna_ocr_en_filas(pal_instructor, tolerancia_y_px, escala),
        "x0_instructor": x1_res / escala,  # en puntos PDF
    }


def _ocr_pagina_datos(pagina):
    """
    Hace UNA sola pasada de OCR sobre la página completa (a DPI_OCR, el
    mismo de siempre — no se sube el costo del Paso 1) usando
    image_to_data en vez de image_to_string. Con esos datos:
      - Se reconstruye el texto de la página en orden de líneas
        (arriba->abajo, izquierda->derecha), lo cual además mejora la
        precisión del matching actual "de regalo".
      - Se arma la estructura de columnas (Resultado/Instructor) para
        que el Paso 2 pueda ubicar y verificar la fila exacta del
        match, sin OCR adicional.

    Devuelve (texto_reconstruido, columnas).
    """
    if not OCR_DISPONIBLE:
        return "", None
    with _OCR_SEMAPHORE:
        matriz = fitz.Matrix(DPI_OCR / 72, DPI_OCR / 72)
        pix = pagina.get_pixmap(matrix=matriz, alpha=False)
        imagen = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        try:
            datos = pytesseract.image_to_data(
                imagen, lang=IDIOMA_OCR, timeout=TIMEOUT_OCR_POR_PAGINA,
                output_type=pytesseract.Output.DICT,
            )
        except RuntimeError as e:
            logger.warning("OCR timeout en página: %s", e)
            return "", None
        except Exception:
            logger.exception("Error en OCR (image_to_data) de página")
            return "", None

    filas_planas = _agrupar_filas_ocr(datos, DPI_OCR)
    texto_reconstruido = "\n".join(f["texto_original"] for f in filas_planas)
    columnas = _extraer_estructura_columnas_ocr(datos, DPI_OCR)
    return texto_reconstruido, columnas


def _extraer_pdf(pdf_path: Path, permitir_ocr: bool):
    """
    Devuelve (paginas, error, ocr_intentado):
      - ocr_intentado=True  → se ejecutó OCR al menos una vez (haya o no
        producido texto). Evita reintentos infinitos sobre PDFs que
        nunca van a dar texto (escaneados, protegidos, corruptos…).
      - ocr_intentado=False → no se intentó OCR, así que una segunda
        llamada con permitir_ocr=True SÍ debe volver a procesarlo.
    """
    t0 = time.time()
    paginas = []
    paginas_con_ocr = 0
    ocr_intentado = False
    try:
        documento = fitz.open(str(pdf_path))
    except Exception as e:
        logger.exception("Error abriendo %s", pdf_path.name)
        return [], f"Error abriendo PDF: {e}", False

    try:
        for numero, pagina in enumerate(documento, start=1):
            t_pag = time.time()
            texto_original = pagina.get_text("text") or ""
            texto_normalizado = normalizar_texto(texto_original)
            columnas = _extraer_estructura_columnas_digital(pagina)
            metodo = "TEXTO"

            if (permitir_ocr and OCR_DISPONIBLE
                    and len(texto_normalizado) < MIN_CARACTERES_TEXTO_PAGINA):
                ocr_intentado = True          # ← aunque falle, ya no reintentamos
                texto_ocr, columnas_ocr = _ocr_pagina_datos(pagina)
                texto_ocr_normalizado = normalizar_texto(texto_ocr)
                if len(texto_ocr_normalizado) > len(texto_normalizado):
                    texto_original = texto_ocr
                    texto_normalizado = texto_ocr_normalizado
                    columnas = columnas_ocr
                    metodo = "OCR"
                    paginas_con_ocr += 1

            paginas.append({
                "pagina": numero,
                "original": texto_original,
                "normalizado": texto_normalizado,
                "metodo": metodo,
                # None si no se pudo ubicar el encabezado de la tabla en
                # esta página (p. ej. página de continuación): en ese
                # caso el Paso 2 simplemente no podrá verificar el
                # marcador y la coincidencia se toma tal cual.
                "columnas": columnas,
            })
            if time.time() - t_pag > 5:
                logger.warning(
                    "  Página %d de %s tardó %.1fs",
                    numero, pdf_path.name, time.time() - t_pag,
                )
    finally:
        documento.close()

    caracteres = sum(len(p["normalizado"]) for p in paginas)
    dur = time.time() - t0
    logger.info(
        "PDF %s: %d páginas, %d chars, %d con OCR (intentado=%s), %.1fs",
        pdf_path.name, len(paginas), caracteres, paginas_con_ocr,
        ocr_intentado, dur,
    )
    if caracteres == 0:
        return [], "Sin texto reconocible (ni digital ni OCR).", ocr_intentado
    return paginas, None, ocr_intentado


def cargar_pdf(pdf_path: Path, permitir_ocr: bool = True):
    """
    Punto de entrada con caché en disco.
    Devuelve (paginas, error, ocr_intentado).
    """
    cache_file = _cache_file_pdf(pdf_path)
    if cache_file is not None and cache_file.exists():
        data = _leer_cache_texto(cache_file)
        if data is not None:
            cache_permitio_ocr = bool(data.get("permitir_ocr", True))
            ocr_intentado = bool(data.get("ocr_intentado", False))
            # Aceptamos la caché si:
            #   - se generó con OCR permitido (es superset de sin-OCR), o
            #   - el caller tampoco pide OCR.
            if cache_permitio_ocr or not permitir_ocr:
                logger.info(
                    "PDF %s: desde caché en disco (permitir_ocr=%s, ocr_intentado=%s)",
                    pdf_path.name, cache_permitio_ocr, ocr_intentado,
                )
                return data["paginas"], data.get("error"), ocr_intentado

    paginas, error, ocr_intentado = _extraer_pdf(pdf_path, permitir_ocr)

    # ⚠️ SIEMPRE escribir la caché, también cuando hay error.
    # Si no, un PDF problemático se reintenta en bucle en cada ejecución.
    if cache_file is not None:
        _escribir_cache_texto(cache_file, {
            "paginas": paginas,
            "error": error,
            "permitir_ocr": permitir_ocr,
            "ocr_intentado": ocr_intentado,
        })
    return paginas, error, ocr_intentado


# ============================================================
# INVENTARIO Y RELACIÓN FICHA -> PDF
# ============================================================

def obtener_todos_los_pdfs(carpeta_bd: Path) -> list[Path]:
    if not carpeta_bd.exists():
        raise FileNotFoundError(f"No existe la carpeta con PDFs: {carpeta_bd}")
    pdfs = [a for a in carpeta_bd.rglob("*") if a.is_file() and a.suffix.lower() == ".pdf"]
    return sorted(set(pdfs), key=lambda p: str(p).lower())


def _hash_inventario(pdfs: list[Path]) -> str:
    h = hashlib.md5()
    for p in sorted(pdfs, key=lambda x: x.name.lower()):
        try:
            st = p.stat()
            h.update(f"{p.name}:{st.st_size}".encode())
        except OSError:
            h.update(f"{p.name}:?".encode())
    return h.hexdigest()


def _ruta_cache_mapa(pdfs: list[Path]) -> Optional[Path]:
    if CACHE_DISCO_DIR is None:
        return None
    return CACHE_DISCO_DIR / f"mapa_{_hash_inventario(pdfs)}.json"


def construir_mapa_ficha_pdf(
    fichas,
    todos_los_pdfs: list[Path],
    cache: PDFCache,
    progress_callback: ProgressCallback = None,
    usar_cache_disco: bool = True,
):
    """
    Asocia cada PDF a TODAS las fichas cuyo número aparezca:
      1) En el NOMBRE del archivo (barato, sin abrir el PDF).
      2) En el CONTENIDO digital del PDF (sin OCR por defecto).
    """
    # -------------------- Caché del mapa en disco --------------------
    cache_mapa = _ruta_cache_mapa(todos_los_pdfs) if usar_cache_disco else None
    if cache_mapa is not None and cache_mapa.exists():
        try:
            with open(cache_mapa, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            por_nombre = {p.name: p for p in todos_los_pdfs}
            mapa = {}
            for ficha in fichas:
                mapa[ficha] = [
                    por_nombre[n] for n in data.get(ficha, []) if n in por_nombre
                ]
            logger.info("Mapa ficha->PDF cargado desde caché en disco (%s)", cache_mapa.name)
            return mapa, []
        except Exception:
            logger.exception("Caché de mapa corrupta, se reconstruye: %s", cache_mapa)

    # -------------------- Preparar dígitos de cada ficha --------------------
    fichas_digitos: dict[str, str] = {}
    for f in fichas:
        digitos = re.sub(r"\D", "", str(f))
        if digitos:
            fichas_digitos[digitos] = f

    patron_numero = re.compile(rf"(?<!\d)(\d{{{MIN_DIGITOS_FICHA},}})(?!\d)")
    mapa: dict[str, list[Path]] = {f: [] for f in fichas}

    # -------------------- Fase 1: indexar por nombre de archivo --------------------
    t = time.time()
    pdfs_pendientes: list[Path] = []
    for pdf in todos_los_pdfs:
        digitos_nombre = set(patron_numero.findall(pdf.stem))
        hits = [fichas_digitos[d] for d in digitos_nombre if d in fichas_digitos]
        if hits:
            for ficha in hits:
                mapa[ficha].append(pdf)
        else:
            pdfs_pendientes.append(pdf)
    logger.info(
        "Indexado por nombre: %d PDFs resueltos, %d pendientes (%.1fs)",
        len(todos_los_pdfs) - len(pdfs_pendientes), len(pdfs_pendientes),
        time.time() - t,
    )

    # -------------------- Fase 2: indexar por contenido (sin OCR) --------------------
    pdfs_omitidos: list[Path] = []
    if pdfs_pendientes:
        cache.preload(
            pdfs_pendientes,
            progress_callback=progress_callback,
            permitir_ocr=OCR_EN_INDEXADO,
        )
        t = time.time()
        for pdf in pdfs_pendientes:
            paginas, _ = cache.get(pdf, permitir_ocr=OCR_EN_INDEXADO)
            if not paginas:
                pdfs_omitidos.append(pdf)
                continue
            texto_total = " ".join(p["normalizado"] for p in paginas)
            numeros_encontrados = set(patron_numero.findall(texto_total))
            hits = [fichas_digitos[d] for d in numeros_encontrados if d in fichas_digitos]
            if not hits:
                pdfs_omitidos.append(pdf)
                continue
            for ficha in hits:
                mapa[ficha].append(pdf)
        logger.info("Indexado por contenido: %.1fs", time.time() - t)

    logger.info(
        "Mapa ficha->PDF: %d fichas con PDFs, %d PDFs indexados, %d omitidos",
        sum(1 for v in mapa.values() if v),
        len(todos_los_pdfs), len(pdfs_omitidos),
    )

    # -------------------- Guardar en caché de disco --------------------
    if cache_mapa is not None:
        try:
            with open(cache_mapa, "w", encoding="utf-8") as fh:
                json.dump(
                    {f: [p.name for p in pdfs] for f, pdfs in mapa.items()},
                    fh,
                )
        except Exception:
            logger.exception("No se pudo guardar la caché de mapa")

    return mapa, pdfs_omitidos


# ============================================================
# CTRL+F + SIMILITUD CONSERVADORA (rapidfuzz)
# ============================================================

def buscar_exactamente_en_pagina(texto_pagina, frase):
    return bool(texto_pagina and frase and frase in texto_pagina)


def obtener_fragmento(texto_original, frase):
    if not texto_original:
        return ""
    texto_n = normalizar_texto(texto_original)
    pos = texto_n.find(frase)
    if pos >= 0:
        return texto_n[max(0, pos - 180):pos + len(frase) + 220].strip()
    return texto_n[:500].strip()


def construir_ventanas(texto, tamano=80):
    palabras = texto.split()
    if len(palabras) <= tamano:
        return [texto]
    paso = max(20, tamano // 2)
    ventanas = []
    for inicio in range(0, len(palabras), paso):
        fin = min(len(palabras), inicio + tamano)
        ventanas.append(" ".join(palabras[inicio:fin]))
        if fin == len(palabras):
            break
    return ventanas


def calcular_similitud_conservadora(texto_pagina, frase):
    frase = normalizar_texto(frase)
    texto_pagina = normalizar_texto(texto_pagina)
    if not frase or not texto_pagina:
        return False, 0.0, ""

    palabras_frase = palabras_importantes(frase)
    if len(palabras_frase) < 3:
        return False, 0.0, ""

    mejor_global = 0.0
    mejor_cobertura = 0.0

    for ventana in construir_ventanas(texto_pagina, TAMANO_VENTANA_PALABRAS):
        # rapidfuzz: ratio devuelve 0-100
        similitud = fuzz.ratio(frase, ventana) / 100.0
        palabras_ventana = ventana.split()
        longitud_frase = max(3, len(frase.split()))

        mejor_local = similitud
        for i in range(max(1, len(palabras_ventana) - longitud_frase + 1)):
            fragmento = " ".join(palabras_ventana[i:i + longitud_frase])
            score = fuzz.ratio(frase, fragmento) / 100.0
            if score > mejor_local:
                mejor_local = score

        encontradas = 0
        for palabra in palabras_frase:
            mejor_palabra = 0.0
            for palabra_doc in palabras_ventana:
                if palabra == palabra_doc:
                    mejor_palabra = 1.0
                    break
                if len(palabra) >= 5 and len(palabra_doc) >= 5:
                    score = fuzz.ratio(palabra, palabra_doc) / 100.0
                    if score > mejor_palabra:
                        mejor_palabra = score
            if mejor_palabra >= UMBRAL_PALABRA:
                encontradas += 1

        cobertura = encontradas / len(palabras_frase)
        mejor_global = max(mejor_global, mejor_local)
        mejor_cobertura = max(mejor_cobertura, cobertura)

    coincide = (mejor_global >= UMBRAL_SIMILITUD_VENTANA or
                (mejor_cobertura >= UMBRAL_COBERTURA and len(palabras_frase) >= 4))

    if not coincide:
        return False, max(mejor_global, mejor_cobertura), ""

    tipo = ("SIMILITUD ALTA" if mejor_global >= UMBRAL_SIMILITUD_VENTANA
            else "COBERTURA ALTA DE PALABRAS")
    return True, max(mejor_global, mejor_cobertura), tipo


# ============================================================
# PASO 2: UBICAR LA FILA DEL MATCH Y VERIFICAR MARCADOR
# ============================================================

def _construir_texto_indexado(fragmentos):
    """
    Concatena el texto normalizado de los fragmentos de UNA columna (en
    orden), y devuelve también, por cada fragmento con texto, el rango
    [inicio, fin) que ocupa en ese texto concatenado. Sirve para, dada
    una posición de match, saber qué fragmento(s) la generaron.
    """
    texto = ""
    rangos = []  # (inicio, fin, indice_fragmento)
    for idx, frag in enumerate(fragmentos):
        t = frag.get("texto_normalizado") or ""
        if not t:
            continue
        inicio = len(texto) + (1 if texto else 0)
        texto = f"{texto} {t}" if texto else t
        rangos.append((inicio, len(texto), idx))
    return texto, rangos


def _filas_para_rango(rangos, num_fragmentos, pos_inicio, pos_fin, margen_filas=1):
    indices = [idx for (ini, fin, idx) in rangos if fin > pos_inicio and ini < pos_fin]
    if not indices:
        return None
    primero = max(0, min(indices) - margen_filas)
    ultimo = min(num_fragmentos - 1, max(indices) + margen_filas)
    return primero, ultimo


def _localizar_filas_del_match(fragmentos, frase, margen_filas: int = 0):
    """
    Ubica qué fragmento(s) de la columna "Resultado de aprendizaje"
    (reconstruidos por _extraer_estructura_columnas_digital /
    _extraer_estructura_columnas_ocr) generaron el match de `frase`,
    para poder ubicar la fila exacta en el Paso 2.

    margen_filas se deja en 0 por defecto a propósito: sumar
    fragmentos ENTEROS de más podría arrastrar información de otra
    fila de tabla. El margen de tolerancia para no cortar texto por el
    borde se aplica en puntos PDF al recortar (MARGEN_VERIFICACION_PT),
    no sumando fragmentos.

    Devuelve (idx_inicio, idx_fin) o None si no se pudo determinar
    (por ejemplo, página sin columnas detectables).
    """
    if not fragmentos:
        return None

    texto, rangos = _construir_texto_indexado(fragmentos)
    if not texto:
        return None

    # 1) Intento exacto (mismo criterio que buscar_exactamente_en_pagina).
    pos = texto.find(frase)
    if pos >= 0:
        rango = _filas_para_rango(rangos, len(fragmentos), pos, pos + len(frase), margen_filas)
        if rango:
            return rango

    # 2) Intento difuso: ventana deslizante de fragmentos consecutivos,
    #    buscando el grupo más parecido a la frase completa.
    palabras_frase = palabras_importantes(frase)
    if len(palabras_frase) < 3:
        return None

    longitud_objetivo = max(1, len(frase.split()))
    tope_palabras = longitud_objetivo * 2 + 10
    mejor_score, mejor_rango = 0.0, None
    n = len(fragmentos)
    for i in range(n):
        acumulado = ""
        for j in range(i, n):
            t = fragmentos[j].get("texto_normalizado") or ""
            if not t:
                continue
            acumulado = f"{acumulado} {t}".strip()
            if len(acumulado.split()) > tope_palabras:
                break
            score = fuzz.ratio(frase, acumulado) / 100.0
            if score > mejor_score:
                mejor_score, mejor_rango = score, (i, j)

    if mejor_rango and mejor_score >= 0.55:
        i, j = mejor_rango
        return (max(0, i - margen_filas), min(n - 1, j + margen_filas))
    return None


def _localizar_todas_las_filas_del_match(fragmentos, frase, margen_filas: int = 0):
    """
    A diferencia de _localizar_filas_del_match (una sola ubicación),
    enumera TODAS las apariciones EXACTAS de `frase` en el texto
    reconstruido de la columna "Resultado de aprendizaje" de la página.

    Es necesario porque una misma competencia puede repetirse varias
    veces en una misma página — típicamente una fila con instructor
    asignado y otra idéntica "PENDIENTE PARA PROGRAMAR EL PRÓXIMO
    TRIMESTRE" (como ocurre en la ficha 2873758 de ejemplo). Si solo
    verificáramos la primera aparición, una repetición sin instructor
    podría enmascarar incorrectamente otra repetición que sí está
    programada.

    Devuelve una lista de (idx_inicio, idx_fin), una por cada aparición
    exacta encontrada. Si no hay ninguna aparición exacta (por ejemplo
    porque la columna no se pudo reconstruir del todo bien), cae al
    único mejor match difuso.
    """
    if not fragmentos:
        return []

    texto, rangos = _construir_texto_indexado(fragmentos)
    if not texto:
        return []

    apariciones = []
    pos = texto.find(frase)
    while pos >= 0:
        rango = _filas_para_rango(rangos, len(fragmentos), pos, pos + len(frase), margen_filas)
        if rango and rango not in apariciones:
            apariciones.append(rango)
        pos = texto.find(frase, pos + 1)

    if apariciones:
        return apariciones

    rango = _localizar_filas_del_match(fragmentos, frase, margen_filas)
    return [rango] if rango else []


def _contiene_marcador_pendiente(texto_normalizado: str,
                                  umbral: float = UMBRAL_MARCADOR_PENDIENTE) -> bool:
    """
    True si el texto (ya normalizado) contiene, como palabra completa,
    alguno de los marcadores PENDIENTE/INSTRUCTOR — con tolerancia
    difusa para ruido de OCR incluso a 300 DPI.
    """
    if not texto_normalizado:
        return False
    for palabra in texto_normalizado.split():
        if palabra in PALABRAS_MARCADOR_PENDIENTE:
            return True
        if len(palabra) >= 5:
            for marcador in PALABRAS_MARCADOR_PENDIENTE:
                if fuzz.ratio(palabra, marcador) / 100.0 >= umbral:
                    return True
    return False


def _recortar_y_ocr_verificacion(pdf_path: Path, numero_pagina: int,
                                  y0: float, y1: float,
                                  x0: Optional[float] = None,
                                  margen_pt: float = MARGEN_VERIFICACION_PT) -> str:
    """
    Reabre el PDF, recorta SOLO la franja [y0-margen, y1+margen] de la
    página indicada —desde x0 hasta el borde derecho si se da x0 (para
    acotar a la columna de Instructor), o ancho completo si no— y le
    hace OCR a DPI_OCR_VERIFICACION (por defecto 300). Es barato porque
    es una franja pequeña de una sola página, no la página completa ni
    el documento entero.

    Devuelve el texto normalizado de esa franja, o "" si algo falla
    (PDF ilegible, OCR no disponible, rango inválido, etc.) — en cuyo
    caso el llamador debe tratarlo como "no se detectó marcador" para
    no bloquear coincidencias válidas por un error de infraestructura.
    """
    if not OCR_DISPONIBLE:
        return ""
    try:
        documento = fitz.open(str(pdf_path))
    except Exception:
        logger.exception("Verificación: no se pudo reabrir %s", pdf_path.name)
        return ""
    try:
        if numero_pagina < 1 or numero_pagina > len(documento):
            return ""
        pagina = documento[numero_pagina - 1]
        rect = pagina.rect
        x_izq = rect.x0 if x0 is None else max(rect.x0, x0 - margen_pt)
        clip = fitz.Rect(
            x_izq,
            max(rect.y0, y0 - margen_pt),
            rect.x1,
            min(rect.y1, y1 + margen_pt),
        )
        if clip.width <= 0 or clip.height <= 0:
            return ""

        matriz = fitz.Matrix(DPI_OCR_VERIFICACION / 72, DPI_OCR_VERIFICACION / 72)
        pix = pagina.get_pixmap(matrix=matriz, clip=clip, alpha=False)
        imagen = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        with _OCR_SEMAPHORE:
            texto = pytesseract.image_to_string(
                imagen, lang=IDIOMA_OCR, timeout=TIMEOUT_OCR_POR_PAGINA
            ) or ""
        return normalizar_texto(texto)
    except Exception:
        logger.exception(
            "Verificación: error recortando/OCR en %s página %d",
            pdf_path.name, numero_pagina,
        )
        return ""
    finally:
        documento.close()


def ruta_trazabilidad(pdf_path: Path, carpeta_bd: Path):
    try:
        return str(pdf_path.relative_to(carpeta_bd))
    except ValueError:
        return str(pdf_path)


MARGEN_ALINEACION_FILA_PT = float(os.environ.get("NP_MARGEN_ALINEACION_PT") or 6.0)


def _texto_instructor_alineado(columnas: dict, y0: float, y1: float,
                                margen: float = MARGEN_ALINEACION_FILA_PT) -> str:
    """
    Dado el rango vertical [y0,y1] donde matcheó la competencia en la
    columna "Resultado de aprendizaje", concatena el texto (ya
    normalizado, texto digital) de los fragmentos de la columna
    "Instructor" cuyo rango vertical se solapa con ese, es decir, el
    contenido de la celda "Instructor" de esa misma fila de tabla.
    """
    fragmentos = columnas.get("instructor") or []
    solapados = [
        f for f in fragmentos
        if f["y1"] >= y0 - margen and f["y0"] <= y1 + margen
    ]
    return " ".join(f["texto_normalizado"] for f in solapados if f["texto_normalizado"])


def _verificar_marcador_para_ocurrencia(pdf_path: Path, pagina: dict, rango) -> tuple:
    """
    Dada una página ya extraída (con su "columnas") y el rango de
    fragmentos de la columna "Resultado" donde matcheó la frase,
    determina si esa fila tiene el marcador PENDIENTE/INSTRUCTOR.

    - Si la página es de texto digital (metodo == "TEXTO"), el texto de
      la columna Instructor ya es confiable: se verifica directo, SIN
      OCR adicional (más rápido y más preciso que cualquier OCR).
    - Si la página vino de OCR (metodo == "OCR", probablemente a 150
      DPI), se recorta SOLO la franja de la columna Instructor
      alineada con esa fila y se le hace OCR de nuevo a
      DPI_OCR_VERIFICACION para una lectura confiable.

    Devuelve (marcador_detectado: bool, texto_verificacion: str).
    """
    columnas = pagina.get("columnas")
    if rango is None or not columnas:
        return False, ""

    i, j = rango
    fragmentos_resultado = columnas.get("resultado") or []
    if i >= len(fragmentos_resultado) or j >= len(fragmentos_resultado):
        return False, ""
    y0 = fragmentos_resultado[i]["y0"]
    y1 = fragmentos_resultado[j]["y1"]

    if pagina["metodo"] == "TEXTO":
        texto_instructor = _texto_instructor_alineado(columnas, y0, y1)
        return _contiene_marcador_pendiente(texto_instructor), texto_instructor

    # metodo == "OCR": recorte + re-OCR a alta resolución, acotado a la
    # columna Instructor (más preciso y más barato que ancho completo).
    x0_instructor = columnas.get("x0_instructor")
    texto_verificacion = _recortar_y_ocr_verificacion(
        pdf_path, pagina["pagina"], y0, y1, x0=x0_instructor
    )
    return _contiene_marcador_pendiente(texto_verificacion), texto_verificacion


def analizar_pdf(pdf_path: Path, frase: str, cache: PDFCache, carpeta_bd: Path):
    ruta = ruta_trazabilidad(pdf_path, carpeta_bd)
    # >>> En la fase de análisis SÍ permitimos OCR
    paginas, obs = cache.get(pdf_path, permitir_ocr=True)

    if not paginas:
        return [], obs

    # Cada elemento: (dict_coincidencia_base, pagina, rango_en_columna_resultado|None)
    candidatos = []

    for pagina in paginas:
        if buscar_exactamente_en_pagina(pagina["normalizado"], frase):
            columnas = pagina.get("columnas")
            fragmentos_resultado = (columnas or {}).get("resultado") or []
            # >>> Enumerar TODAS las apariciones de la frase en la
            # columna "Resultado de aprendizaje" de esta página, no
            # solo la primera (una misma competencia puede repetirse
            # con y sin instructor asignado en la misma página).
            apariciones = _localizar_todas_las_filas_del_match(fragmentos_resultado, frase)
            base = {
                "archivo": pdf_path.name, "ruta": ruta, "pagina": pagina["pagina"],
                "metodo_lectura": pagina["metodo"], "tipo": "EXACTA (CTRL+F)",
                "puntuacion": 1.0,
                "evidencia": obtener_fragmento(pagina["original"], frase),
            }
            if not apariciones:
                candidatos.append((dict(base), pagina, None))
            else:
                for rango in apariciones:
                    candidatos.append((dict(base), pagina, rango))

    if not candidatos:
        for pagina in paginas:
            coincide, puntuacion, tipo = calcular_similitud_conservadora(
                pagina["normalizado"], frase
            )
            if coincide:
                columnas = pagina.get("columnas")
                fragmentos_resultado = (columnas or {}).get("resultado") or []
                rango = _localizar_filas_del_match(fragmentos_resultado, frase)
                base = {
                    "archivo": pdf_path.name, "ruta": ruta, "pagina": pagina["pagina"],
                    "metodo_lectura": pagina["metodo"], "tipo": tipo,
                    "puntuacion": puntuacion,
                    "evidencia": pagina["normalizado"][:700],
                }
                candidatos.append((base, pagina, rango))

    if not candidatos:
        return [], None

    # ------------------------------------------------------------
    # Paso 2 (solo si hubo match): para cada aparición ya ubicada en la
    # columna "Resultado", verificar si la celda "Instructor" de esa
    # misma fila trae el marcador PENDIENTE/INSTRUCTOR. En páginas de
    # texto digital esto es gratis (ya se tiene el texto exacto); en
    # páginas de OCR se recorta+re-OCR SOLO esa franja a alta
    # resolución. No se toca el resto de la página ni del documento.
    # ------------------------------------------------------------
    coincidencias = []
    for base, pagina, rango in candidatos:
        if rango is None:
            logger.warning(
                "No se pudo ubicar la fila del match en %s página %d "
                "(frase: %.60s…); se toma el match sin verificar marcador.",
                pdf_path.name, base["pagina"], frase,
            )
            marcador_detectado, texto_verificacion = False, ""
        else:
            marcador_detectado, texto_verificacion = _verificar_marcador_para_ocurrencia(
                pdf_path, pagina, rango
            )
        base["marcador_pendiente"] = marcador_detectado
        base["texto_verificacion"] = texto_verificacion
        coincidencias.append(base)

    return coincidencias, None


# ============================================================
# PROCESAR UNA FICHA
# ============================================================

def analizar_ficha(ficha, filas_validas, pdfs_de_la_ficha, cache: PDFCache, carpeta_bd):
    resultados, observaciones, archivos, detalles, evidencias = {}, {}, {}, {}, {}

    for fila, codigo, frase, valor_original in filas_validas:
        if frase in COMPETENCIAS_FORZADAS_PROGRAMADO:
            resultados[fila] = True
            observaciones[fila] = "Forzado como PROGRAMADO (competencia administrativa, no requiere PDF)."
            archivos[fila], detalles[fila], evidencias[fila] = ["FORZADO — no se buscó en PDF"], [], []
            continue

        if not pdfs_de_la_ficha:
            resultados[fila] = False
            observaciones[fila] = "Sin PDF asociado a esta ficha."
            archivos[fila], detalles[fila], evidencias[fila] = ["SIN PDF ASOCIADO"], [], []
            continue

        encontrados = []
        encontrados_con_marcador = []
        for pdf in pdfs_de_la_ficha:
            coincidencias, obs = analizar_pdf(pdf, frase, cache, carpeta_bd)
            for c in coincidencias:
                if c.get("marcador_pendiente"):
                    encontrados_con_marcador.append(c)
                else:
                    encontrados.append(c)

        # Solo cuentan como PROGRAMADO las coincidencias sin marcador.
        resultados[fila] = bool(encontrados)

        if encontrados:
            archivos[fila] = [
                f"{c['ruta']} | Página {c['pagina']} ({c['metodo_lectura']})"
                for c in encontrados
            ]
            detalles[fila] = [f"{c['puntuacion']:.0%} | {c['tipo']}" for c in encontrados]
            evidencias[fila] = [c["evidencia"] for c in encontrados if c.get("evidencia")]
            observaciones[fila] = (
                f"Encontrada en {encontrados[0]['ruta']} ({encontrados[0]['tipo']})."
            )
        elif encontrados_con_marcador:
            # Hubo match(es) de la competencia, pero todos en filas con
            # instructor pendiente/sin asignar → no cuenta como programado,
            # pero queda trazado (no se pierde la evidencia del hallazgo).
            rutas_con_marcador = {c["ruta"] for c in encontrados_con_marcador}
            rutas_revisadas = [ruta_trazabilidad(pdf, carpeta_bd) for pdf in pdfs_de_la_ficha]
            archivos[fila] = (
                [
                    f"{c['ruta']} | Página {c['pagina']} ({c['metodo_lectura']}) "
                    "— INSTRUCTOR PENDIENTE"
                    for c in encontrados_con_marcador
                ]
                + [
                    f"{r} (revisado, sin coincidencia válida)"
                    for r in rutas_revisadas if r not in rutas_con_marcador
                ]
            )
            detalles[fila] = [
                f"{c['puntuacion']:.0%} | {c['tipo']} | INSTRUCTOR PENDIENTE"
                for c in encontrados_con_marcador
            ]
            evidencias[fila] = [
                c["texto_verificacion"] for c in encontrados_con_marcador
                if c.get("texto_verificacion")
            ]
            primero = encontrados_con_marcador[0]
            observaciones[fila] = (
                f"Encontrada en {primero['ruta']} (página {primero['pagina']}) "
                "pero con instructor pendiente/sin asignar — no cuenta como programado."
            )
        else:
            rutas_revisadas = [ruta_trazabilidad(pdf, carpeta_bd) for pdf in pdfs_de_la_ficha]
            archivos[fila] = [f"{r} (revisado, sin coincidencia)" for r in rutas_revisadas]
            detalles[fila], evidencias[fila] = [], []
            observaciones[fila] = (
                "No encontrada. PDF(s) revisado(s) de esta ficha: "
                + ", ".join(rutas_revisadas)
            )

    return resultados, observaciones, archivos, detalles, evidencias


# ============================================================
# REORDENAR FILAS + HOJA DE REPORTE
# ============================================================

def reordenar_filas_por_estado(ws, resultados):
    filas = sorted(resultados.keys())
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
    doc_actual = ws.cell(filas[0], COL_DOCUMENTO).value
    for fila in filas[1:]:
        doc = ws.cell(fila, COL_DOCUMENTO).value
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
        orden_nuevo = sorted(bloque, key=lambda f: 0 if resultados[f] else 1)
        for posicion, fila_origen in zip(bloque, orden_nuevo):
            aplicar_fila(posicion, snapshots[fila_origen])


def escribir_hoja_reporte(wb, datos_fichas, resultados_globales, observ_globales,
                           archivos_globales, detalles_globales, evidencias_globales):
    if "Reporte" in wb.sheetnames:
        del wb["Reporte"]
    ws = wb.create_sheet("Reporte")

    encabezados = ["Ficha", "Fila", "Código", "Competencia (Excel)", "Estado",
                   "Observación", "Archivo(s) / Página", "Tipo / Similitud",
                   "Texto encontrado para verificar"]
    ws.append(encabezados)

    for ficha, filas_validas in datos_fichas.items():
        resultados = resultados_globales.get(ficha, {})
        observaciones = observ_globales.get(ficha, {})
        archivos = archivos_globales.get(ficha, {})
        detalles = detalles_globales.get(ficha, {})
        evidencias = evidencias_globales.get(ficha, {})

        for fila, codigo, frase, valor_original in filas_validas:
            estado = "PROGRAMADO" if resultados.get(fila) else "NO PROGRAMADO"
            texto_evidencia = "\n---\n".join(evidencias.get(fila, []))
            if len(texto_evidencia) > 5000:
                texto_evidencia = texto_evidencia[:5000] + "\n..."

            ws.append([
                ficha, fila, codigo or "", valor_original, estado,
                observaciones.get(fila, ""), "\n".join(archivos.get(fila, [])),
                "\n".join(detalles.get(fila, [])), texto_evidencia,
            ])

    for fila in range(2, ws.max_row + 1):
        for col in (6, 7, 8, 9):
            ws.cell(fila, col).alignment = openpyxl.styles.Alignment(
                wrap_text=True, vertical="top"
            )

    anchos = {"A": 14, "B": 8, "C": 10, "D": 45, "E": 16,
              "F": 45, "G": 40, "H": 26, "I": 60}
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
    logger.info("Excel: %s", archivo_excel)
    logger.info("Carpeta BD: %s", carpeta_bd)
    logger.info("Salida: %s", salida_dir)

    if not archivo_excel.exists():
        raise FileNotFoundError(f"No se encontró el Excel: {archivo_excel}")

    salida_dir.mkdir(parents=True, exist_ok=True)

    _notificar(progress_callback, "Abriendo Excel y listando PDFs…", 0, 1)

    t = time.time()
    wb = openpyxl.load_workbook(archivo_excel)
    logger.info("Excel abierto en %.1fs (%d hojas)", time.time() - t, len(wb.worksheets))

    todos_los_pdfs = obtener_todos_los_pdfs(carpeta_bd)
    logger.info("PDFs encontrados: %d", len(todos_los_pdfs))
    if not todos_los_pdfs:
        raise FileNotFoundError(f"No hay PDFs en: {carpeta_bd}")

    datos_fichas = {}
    for ws in wb.worksheets:
        ficha = str(ws.title).strip()
        filas_validas = []
        for fila in range(FILA_INICIO_DATOS, ws.max_row + 1):
            if not fila_es_valida(ws, fila):
                continue
            valor_original = ws.cell(fila, COL_COMPETENCIA).value
            codigo, frase = extraer_competencia(valor_original)
            if frase:
                filas_validas.append((fila, codigo, frase, valor_original))
        datos_fichas[ficha] = filas_validas

    total_fichas = len(datos_fichas)
    logger.info("Fichas (hojas) con filas válidas: %d", total_fichas)

    _notificar(
        progress_callback,
        f"Buscando números de ficha dentro de {len(todos_los_pdfs)} PDF(s)…",
        0, 1,
    )

    cache = PDFCache(max_workers=NUM_WORKERS_PDF, max_size=PDF_CACHE_MAX_SIZE)
    try:
        # 1) Indexado (mapa ficha -> PDFs). Reporta progreso internamente.
        t = time.time()
        mapa_ficha_pdf, pdfs_omitidos = construir_mapa_ficha_pdf(
            list(datos_fichas.keys()),
            todos_los_pdfs,
            cache=cache,
            progress_callback=progress_callback,
        )
        logger.info("Fase de indexado: %.1fs", time.time() - t)

        fichas_sin_pdf = [f for f, pdfs in mapa_ficha_pdf.items() if not pdfs]
        fichas_con_pdf = [f for f, pdfs in mapa_ficha_pdf.items() if pdfs]
        logger.info(
            "Fichas con PDF: %d, sin PDF: %d",
            len(fichas_con_pdf), len(fichas_sin_pdf),
        )

        resultados_globales, observ_globales = {}, {}
        archivos_globales, detalles_globales, evidencias_globales = {}, {}, {}

        # 2) Análisis de fichas.
        _notificar(
            progress_callback,
            f"Analizando {total_fichas} fichas…",
            0, total_fichas,
        )

        t = time.time()
        completadas = 0
        with ThreadPoolExecutor(
            max_workers=NUM_WORKERS_FICHAS, thread_name_prefix="np-ficha"
        ) as pool:
            futuros = {}
            for ficha, filas_validas in datos_fichas.items():
                fut = pool.submit(
                    analizar_ficha,
                    ficha,
                    filas_validas,
                    mapa_ficha_pdf.get(ficha, []),
                    cache,
                    carpeta_bd,
                )
                futuros[fut] = ficha

            for fut in as_completed(futuros):
                ficha = futuros[fut]
                try:
                    r, o, a, d, e = fut.result()
                except Exception:
                    resultados_globales[ficha] = {}
                    observ_globales[ficha] = {}
                    archivos_globales[ficha] = {}
                    detalles_globales[ficha] = {}
                    evidencias_globales[ficha] = {}
                    logger.exception("Error en ficha %s", ficha)
                else:
                    resultados_globales[ficha] = r
                    observ_globales[ficha] = o
                    archivos_globales[ficha] = a
                    detalles_globales[ficha] = d
                    evidencias_globales[ficha] = e

                completadas += 1
                if completadas % 5 == 0 or completadas == total_fichas:
                    seg = time.time() - t
                    logger.info(
                        "Análisis: %d/%d fichas (%.1fs, %.2f ficha/s)",
                        completadas, total_fichas, seg,
                        completadas / seg if seg > 0 else 0,
                    )
                _notificar(
                    progress_callback,
                    f"Ficha {ficha} analizada ({completadas}/{total_fichas})…",
                    completadas, total_fichas,
                )

        logger.info("Fase de análisis: %.1fs", time.time() - t)

        total_programado = 0
        total_no_programado = 0

        for ws in wb.worksheets:
            ficha = str(ws.title).strip()
            resultados = resultados_globales.get(ficha, {})

            ws.cell(4, COL_ESTADO).value = "Estado Programación"
            for col in (COL_OBSERVACION, COL_ARCHIVOS, COL_DETALLE, COL_EVIDENCIA):
                ws.cell(4, col).value = None

            for fila in range(FILA_INICIO_DATOS, ws.max_row + 1):
                for col in (COL_OBSERVACION, COL_ARCHIVOS, COL_DETALLE, COL_EVIDENCIA):
                    ws.cell(fila, col).value = None
                if fila not in resultados:
                    continue
                if resultados[fila]:
                    ws.cell(fila, COL_ESTADO).value = "PROGRAMADO"
                    total_programado += 1
                else:
                    ws.cell(fila, COL_ESTADO).value = "NO PROGRAMADO"
                    total_no_programado += 1

            reordenar_filas_por_estado(ws, resultados)
    finally:
        cache.shutdown()

    _notificar(progress_callback, "Generando hoja de reporte…", total_fichas, total_fichas)
    escribir_hoja_reporte(wb, datos_fichas, resultados_globales, observ_globales,
                           archivos_globales, detalles_globales, evidencias_globales)

    ruta_salida = salida_dir / "Consolidado_Procesado.xlsx"
    wb.save(ruta_salida)

    # --- Resumen de tiempos en una sola línea (para verificar en Render) ---
    logger.info(
        "RESUMEN | total=%.1fs | fichas=%d | pdfs=%d | programado=%d | no_programado=%d "
        "| cache_disco=%s | ocr_indexado=%s",
        time.time() - t_inicio, total_fichas, len(todos_los_pdfs),
        total_programado, total_no_programado,
        bool(CACHE_DISCO_DIR), OCR_EN_INDEXADO,
    )

    _notificar(progress_callback, "Listo.", total_fichas, total_fichas)

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(carpeta_bd))
        except ValueError:
            return str(p)

    return {
        "archivo_generado": str(ruta_salida),
        "ocr_disponible": OCR_DISPONIBLE,
        "total_fichas": total_fichas,
        "total_pdfs_detectados": len(todos_los_pdfs),
        "total_pdfs_omitidos": len(pdfs_omitidos),
        "fichas_sin_pdf": fichas_sin_pdf,
        "fichas_con_pdf": fichas_con_pdf,
        "total_programado": total_programado,
        "total_no_programado": total_no_programado,
        "mapa_ficha_pdf": {
            ficha: [_rel(p) for p in pdfs]
            for ficha, pdfs in mapa_ficha_pdf.items()
            if pdfs
        },
        "pdfs_omitidos": [_rel(p) for p in pdfs_omitidos],
    }