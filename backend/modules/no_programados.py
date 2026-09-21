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
    "ocr=%s ocr_indexado=%s dpi=%d timeout_pdf=%.0fs timeout_ocr=%.0fs disco=%s",
    NUM_WORKERS_PDF, NUM_WORKERS_FICHAS, MAX_OCR_CONCURRENTES,
    PDF_CACHE_MAX_SIZE or "ilimitado", OCR_DISPONIBLE, OCR_EN_INDEXADO, DPI_OCR,
    TIMEOUT_PDF_PRELOAD, TIMEOUT_OCR_POR_PAGINA, CACHE_DISCO_DIR,
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
        f"{pdf_path.name}:{st.st_size}:{int(st.st_mtime)}".encode()
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

def _ocr_pagina(pagina) -> str:
    if not OCR_DISPONIBLE:
        return ""
    with _OCR_SEMAPHORE:
        matriz = fitz.Matrix(DPI_OCR / 72, DPI_OCR / 72)
        pix = pagina.get_pixmap(matrix=matriz, alpha=False)
        imagen = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        try:
            return pytesseract.image_to_string(
                imagen, lang=IDIOMA_OCR, timeout=TIMEOUT_OCR_POR_PAGINA
            ) or ""
        except RuntimeError as e:
            logger.warning("OCR timeout en página: %s", e)
            return ""


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
            metodo = "TEXTO"

            if (permitir_ocr and OCR_DISPONIBLE
                    and len(texto_normalizado) < MIN_CARACTERES_TEXTO_PAGINA):
                ocr_intentado = True          # ← aunque falle, ya no reintentamos
                texto_ocr = _ocr_pagina(pagina)
                texto_ocr_normalizado = normalizar_texto(texto_ocr)
                if len(texto_ocr_normalizado) > len(texto_normalizado):
                    texto_original = texto_ocr
                    texto_normalizado = texto_ocr_normalizado
                    metodo = "OCR"
                    paginas_con_ocr += 1

            paginas.append({
                "pagina": numero,
                "original": texto_original,
                "normalizado": texto_normalizado,
                "metodo": metodo,
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


def ruta_trazabilidad(pdf_path: Path, carpeta_bd: Path):
    try:
        return str(pdf_path.relative_to(carpeta_bd))
    except ValueError:
        return str(pdf_path)


def analizar_pdf(pdf_path: Path, frase: str, cache: PDFCache, carpeta_bd: Path):
    ruta = ruta_trazabilidad(pdf_path, carpeta_bd)
    # >>> En la fase de análisis SÍ permitimos OCR
    paginas, obs = cache.get(pdf_path, permitir_ocr=True)

    if not paginas:
        return [], obs

    coincidencias = []
    for pagina in paginas:
        if buscar_exactamente_en_pagina(pagina["normalizado"], frase):
            coincidencias.append({
                "archivo": pdf_path.name, "ruta": ruta, "pagina": pagina["pagina"],
                "metodo_lectura": pagina["metodo"], "tipo": "EXACTA (CTRL+F)",
                "puntuacion": 1.0,
                "evidencia": obtener_fragmento(pagina["original"], frase),
            })

    if coincidencias:
        return coincidencias, None

    for pagina in paginas:
        coincide, puntuacion, tipo = calcular_similitud_conservadora(
            pagina["normalizado"], frase
        )
        if coincide:
            coincidencias.append({
                "archivo": pdf_path.name, "ruta": ruta, "pagina": pagina["pagina"],
                "metodo_lectura": pagina["metodo"], "tipo": tipo,
                "puntuacion": puntuacion,
                "evidencia": pagina["normalizado"][:700],
            })

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
        for pdf in pdfs_de_la_ficha:
            coincidencias, obs = analizar_pdf(pdf, frase, cache, carpeta_bd)
            encontrados.extend(coincidencias)

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