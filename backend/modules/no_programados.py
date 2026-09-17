"""
modules/no_programados.py

Lógica de NoProgramados.py adaptada para la API web:

- Ya no depende de carpetas fijas (BASE_DIR/BD, BASE_DIR/Consolidado_General.xlsx);
  recibe la ruta del Excel y la carpeta con los PDFs como parámetros.
- Mejora de precisión: la detección de texto vs. OCR ahora se hace PÁGINA POR
  PÁGINA en vez de por PDF completo. Antes, si un PDF tenía la mayoría de
  páginas digitales pero una sola página escaneada, esa página se leía con
  texto vacío y se perdía la competencia que estuviera solo ahí. Ahora cada
  página decide individualmente si necesita OCR.
- Acepta un `progress_callback(mensaje, actual, total)` opcional para poder
  informar avance en tiempo real desde la API.
"""

import re
import unicodedata
from copy import copy
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

import fitz  # PyMuPDF
import openpyxl

# ============================================================
# CONFIGURACIÓN (misma estructura de columnas que el script original)
# ============================================================

COL_CODIGO = 1
COL_COMPETENCIA = 8
FILA_INICIO_DATOS = 5

COL_ESTADO = 11
COL_OBSERVACION = 12
COL_ARCHIVOS = 13
COL_DETALLE = 14
COL_EVIDENCIA = 15
COL_DOCUMENTO = 4  # "Número de Documento": identifica a qué aprendiz pertenece cada fila

IDIOMA_OCR = "spa"
DPI_OCR = 200
MIN_CARACTERES_TEXTO_PAGINA = 20  # umbral por página (antes era por PDF completo)

UMBRAL_SIMILITUD_VENTANA = 0.82
UMBRAL_COBERTURA = 0.80
UMBRAL_PALABRA = 0.88
TAMANO_VENTANA_PALABRAS = 80

COMPETENCIAS_FORZADAS_PROGRAMADO = {
    "RESULTADOS DE APRENDIZAJE ETAPA PRACTICA",
}

PALABRAS_VACIAS = {
    "DE", "DEL", "LA", "LAS", "EL", "LOS", "EN", "Y", "O",
    "A", "AL", "UN", "UNA", "POR", "PARA", "CON", "QUE",
    "SE", "SU", "SUS", "ES", "SON", "UNO",
}

ProgressCallback = Optional[Callable[[str, int, int], None]]

try:
    import pytesseract
    from PIL import Image
    pytesseract.get_tesseract_version()
    OCR_DISPONIBLE = True
except Exception:
    OCR_DISPONIBLE = False


def _notificar(callback: ProgressCallback, mensaje: str, actual: int = 0, total: int = 0):
    if callback:
        callback(mensaje, actual, total)


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
    return SequenceMatcher(None, a, b).ratio()


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
# INVENTARIO Y RELACIÓN FICHA -> PDF
# ============================================================

def obtener_todos_los_pdfs(carpeta_bd: Path) -> list[Path]:
    if not carpeta_bd.exists():
        raise FileNotFoundError(f"No existe la carpeta con PDFs: {carpeta_bd}")
    pdfs = [a for a in carpeta_bd.rglob("*") if a.is_file() and a.suffix.lower() == ".pdf"]
    return sorted(set(pdfs), key=lambda p: str(p).lower())


def construir_mapa_ficha_pdf(fichas, todos_los_pdfs):
    """
    Asocia cada PDF con, como mucho, UNA ficha, recorriendo la lista de
    PDFs una sola vez (en vez de una vez POR CADA ficha, que era el
    cuello de botella real: con F fichas y P pdfs, la versión anterior
    hacía F×P normalizaciones de texto completo de la ruta).

    Devuelve (mapa, pdfs_omitidos): los PDF que no correspondan al número
    de ninguna ficha del Excel se listan en pdfs_omitidos y NUNCA se
    abren ni se les hace OCR — se descartan aquí, antes de la parte cara
    del proceso.
    """
    fichas_norm = {normalizar_texto(f): f for f in fichas if normalizar_texto(f)}
    fichas_digitos = {f: re.sub(r"\D", "", str(f)) for f in fichas}

    mapa = {f: [] for f in fichas}
    pdfs_omitidos = []

    for pdf in todos_los_pdfs:
        partes_norm = [normalizar_texto(p) for p in pdf.parts]

        # 1) Caso ideal y más rápido: alguna carpeta del path coincide
        #    EXACTO con el número de una ficha (ej. BD/2904878/archivo.pdf).
        ficha_encontrada = next((fichas_norm[p] for p in partes_norm if p in fichas_norm), None)

        # 2) Si no hubo coincidencia directa, se revisa si los dígitos de
        #    alguna ficha aparecen en la ruta completa (más lento, pero
        #    solo corre para los PDF que no calzaron con el caso ideal).
        if ficha_encontrada is None:
            ruta_str = str(pdf)
            for ficha, digitos in fichas_digitos.items():
                if digitos and re.search(rf"(?<!\d){re.escape(digitos)}(?!\d)", ruta_str):
                    ficha_encontrada = ficha
                    break

        if ficha_encontrada is not None:
            mapa[ficha_encontrada].append(pdf)
        else:
            pdfs_omitidos.append(pdf)

    return mapa, pdfs_omitidos


# ============================================================
# LECTURA DE PDF — texto y OCR, decidido POR PÁGINA
# ============================================================

def _ocr_pagina(pagina) -> str:
    if not OCR_DISPONIBLE:
        return ""
    matriz = fitz.Matrix(DPI_OCR / 72, DPI_OCR / 72)
    pix = pagina.get_pixmap(matrix=matriz, alpha=False)
    imagen = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return pytesseract.image_to_string(imagen, lang=IDIOMA_OCR) or ""


def cargar_pdf(pdf_path: Path) -> tuple[list[dict], Optional[str]]:
    """
    Devuelve la lista de páginas del PDF, decidiendo página por página si
    hace falta OCR (más preciso que decidirlo para el PDF completo: un PDF
    mixto —algunas páginas digitales, otras escaneadas— queda bien leído
    en ambos tipos de página).
    """
    paginas = []
    try:
        documento = fitz.open(str(pdf_path))
        for numero, pagina in enumerate(documento, start=1):
            texto_original = pagina.get_text("text") or ""
            texto_normalizado = normalizar_texto(texto_original)
            metodo = "TEXTO"

            if len(texto_normalizado) < MIN_CARACTERES_TEXTO_PAGINA and OCR_DISPONIBLE:
                texto_ocr = _ocr_pagina(pagina)
                texto_ocr_normalizado = normalizar_texto(texto_ocr)
                # Se usa el que haya dado más contenido útil (normalmente el OCR
                # en páginas escaneadas, el texto nativo en las digitales).
                if len(texto_ocr_normalizado) > len(texto_normalizado):
                    texto_original, texto_normalizado, metodo = texto_ocr, texto_ocr_normalizado, "OCR"

            paginas.append({
                "pagina": numero,
                "original": texto_original,
                "normalizado": texto_normalizado,
                "metodo": metodo,
            })
        documento.close()
    except Exception as e:
        return [], f"Error abriendo PDF: {e}"

    caracteres = sum(len(p["normalizado"]) for p in paginas)
    if caracteres == 0:
        return [], "Sin texto reconocible (ni digital ni OCR)."
    return paginas, None


# ============================================================
# CTRL+F + SIMILITUD CONSERVADORA (por página, por ventanas)
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
        similitud = SequenceMatcher(None, frase, ventana).ratio()
        palabras_ventana = ventana.split()
        longitud_frase = max(3, len(frase.split()))

        mejor_local = similitud
        for i in range(max(1, len(palabras_ventana) - longitud_frase + 1)):
            fragmento = " ".join(palabras_ventana[i:i + longitud_frase])
            score = SequenceMatcher(None, frase, fragmento).ratio()
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
                    score = similitud_palabra(palabra, palabra_doc)
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

    tipo = "SIMILITUD ALTA" if mejor_global >= UMBRAL_SIMILITUD_VENTANA else "COBERTURA ALTA DE PALABRAS"
    return True, max(mejor_global, mejor_cobertura), tipo


def ruta_trazabilidad(pdf_path: Path, carpeta_bd: Path):
    try:
        return str(pdf_path.relative_to(carpeta_bd))
    except ValueError:
        return str(pdf_path)


def analizar_pdf(pdf_path: Path, frase: str, cache_pdfs: dict, carpeta_bd: Path):
    ruta = ruta_trazabilidad(pdf_path, carpeta_bd)

    if pdf_path not in cache_pdfs:
        cache_pdfs[pdf_path] = cargar_pdf(pdf_path)
    paginas, obs = cache_pdfs[pdf_path]

    if not paginas:
        return [], obs

    coincidencias = []
    for pagina in paginas:
        if buscar_exactamente_en_pagina(pagina["normalizado"], frase):
            coincidencias.append({
                "archivo": pdf_path.name, "ruta": ruta, "pagina": pagina["pagina"],
                "metodo_lectura": pagina["metodo"], "tipo": "EXACTA (CTRL+F)", "puntuacion": 1.0,
                "evidencia": obtener_fragmento(pagina["original"], frase),
            })

    if coincidencias:
        return coincidencias, None

    for pagina in paginas:
        coincide, puntuacion, tipo = calcular_similitud_conservadora(pagina["normalizado"], frase)
        if coincide:
            coincidencias.append({
                "archivo": pdf_path.name, "ruta": ruta, "pagina": pagina["pagina"],
                "metodo_lectura": pagina["metodo"], "tipo": tipo, "puntuacion": puntuacion,
                "evidencia": pagina["normalizado"][:700],
            })

    return coincidencias, None


# ============================================================
# PROCESAR UNA FICHA (solo contra sus propios PDF)
# ============================================================

def analizar_ficha(ficha, filas_validas, pdfs_de_la_ficha, cache_pdfs, carpeta_bd):
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
            coincidencias, obs = analizar_pdf(pdf, frase, cache_pdfs, carpeta_bd)
            encontrados.extend(coincidencias)

        resultados[fila] = bool(encontrados)

        if encontrados:
            archivos[fila] = [f"{c['ruta']} | Página {c['pagina']} ({c['metodo_lectura']})" for c in encontrados]
            detalles[fila] = [f"{c['puntuacion']:.0%} | {c['tipo']}" for c in encontrados]
            evidencias[fila] = [c["evidencia"] for c in encontrados if c.get("evidencia")]
            observaciones[fila] = f"Encontrada en {encontrados[0]['ruta']} ({encontrados[0]['tipo']})."
        else:
            rutas_revisadas = [ruta_trazabilidad(pdf, carpeta_bd) for pdf in pdfs_de_la_ficha]
            archivos[fila] = [f"{r} (revisado, sin coincidencia)" for r in rutas_revisadas]
            detalles[fila], evidencias[fila] = [], []
            observaciones[fila] = "No encontrada. PDF(s) revisado(s) de esta ficha: " + ", ".join(rutas_revisadas)

    return resultados, observaciones, archivos, detalles, evidencias


# ============================================================
# REORDENAR FILAS + HOJA DE REPORTE (idéntico al original)
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
            ws.cell(fila, col).alignment = openpyxl.styles.Alignment(wrap_text=True, vertical="top")

    anchos = {"A": 14, "B": 8, "C": 10, "D": 45, "E": 16, "F": 45, "G": 40, "H": 26, "I": 60}
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
    """
    Ejecuta el proceso completo y devuelve un dict con la ruta del archivo
    generado y un resumen de estadísticas (para mostrar en la web).
    """
    if not archivo_excel.exists():
        raise FileNotFoundError(f"No se encontró el Excel: {archivo_excel}")

    salida_dir.mkdir(parents=True, exist_ok=True)

    _notificar(progress_callback, "Abriendo Excel y listando PDFs…", 0, 1)
    wb = openpyxl.load_workbook(archivo_excel)
    todos_los_pdfs = obtener_todos_los_pdfs(carpeta_bd)
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

    _notificar(progress_callback, f"Relacionando {len(todos_los_pdfs)} PDF(s) con las fichas…", 0, 1)
    mapa_ficha_pdf, pdfs_omitidos = construir_mapa_ficha_pdf(list(datos_fichas.keys()), todos_los_pdfs)

    fichas_sin_pdf = [f for f, pdfs in mapa_ficha_pdf.items() if not pdfs]

    cache_pdfs = {}
    resultados_globales, observ_globales = {}, {}
    archivos_globales, detalles_globales, evidencias_globales = {}, {}, {}

    total_fichas = len(datos_fichas)
    for indice, (ficha, filas_validas) in enumerate(datos_fichas.items(), start=1):
        _notificar(progress_callback, f"Analizando ficha {ficha} ({indice}/{total_fichas})…", indice, total_fichas)
        r, o, a, d, e = analizar_ficha(ficha, filas_validas, mapa_ficha_pdf.get(ficha, []), cache_pdfs, carpeta_bd)
        resultados_globales[ficha] = r
        observ_globales[ficha] = o
        archivos_globales[ficha] = a
        detalles_globales[ficha] = d
        evidencias_globales[ficha] = e

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

    _notificar(progress_callback, "Generando hoja de reporte…", total_fichas, total_fichas)
    escribir_hoja_reporte(wb, datos_fichas, resultados_globales, observ_globales,
                           archivos_globales, detalles_globales, evidencias_globales)

    ruta_salida = salida_dir / "Consolidado_Procesado.xlsx"
    wb.save(ruta_salida)

    _notificar(progress_callback, "Listo.", total_fichas, total_fichas)

    return {
        "archivo_generado": str(ruta_salida),
        "ocr_disponible": OCR_DISPONIBLE,
        "total_fichas": total_fichas,
        "total_pdfs_detectados": len(todos_los_pdfs),
        "total_pdfs_omitidos": len(pdfs_omitidos),
        "fichas_sin_pdf": fichas_sin_pdf,
        "total_programado": total_programado,
        "total_no_programado": total_no_programado,
    }