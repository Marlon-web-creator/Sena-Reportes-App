"""
modules/correo_aprendices.py

Genera el correo electrónico de cada aprendiz de forma PROCEDURAL, a partir
de los datos del propio consolidado (nombres, apellidos y número de
documento). Ya no se cruza contra archivos .xls externos.

Tampoco depende de columnas fijas: en cada hoja se busca la fila de
encabezados (por el texto de las cabeceras) y desde ahí se ubican las
columnas de "Número de Documento", "Nombres" y "Apellidos". El correo se
escribe en una columna nueva al final de los encabezados (o se reutiliza
la columna "Correo Electrónico" si ya existe).

La forma del correo se define con una plantilla, por ejemplo:

    "{inicial}{apellido1}{doc2}"  ->  cchoconta93   (CHRISBEL CHOCONTA ... 1000239293)

Variables disponibles en la plantilla:
    {nombre1}    primer nombre                      chrisbel
    {nombre2}    segundo nombre (vacío si no hay)   yessenia
    {inicial}    inicial del primer nombre          c
    {iniciales}  iniciales de todos los nombres     cy
    {apellido1}  primer apellido                    choconta
    {apellido2}  segundo apellido (vacío si no hay) rojas
    {doc}        documento completo                 1000239293
    {doc2} {doc3} {doc4}  últimos 2, 3 o 4 dígitos  93 / 293 / 9293
"""

import re
import unicodedata
from copy import copy
from pathlib import Path

from openpyxl.utils import get_column_letter

from modules.file_utils import cargar_workbook_compatible

# ---------------------------------------------------------------------------
# Configuración por defecto
# ---------------------------------------------------------------------------
DOMINIO_DEFECTO = "soy.sena.edu.co"
PLANTILLA_DEFECTO = "{inicial}{apellido1}{doc2}"
TITULO_COLUMNA_CORREO = "Correo Electrónico"
MAX_FILAS_BUSQUEDA_ENCABEZADO = 30

# Partículas que forman parte de un apellido compuesto ("DE LA CRUZ", "DEL RIO")
_PARTICULAS = {"de", "del", "la", "las", "los", "san", "santa", "da", "di", "van", "von"}

# Nombres posibles de cada columna (ya normalizados: sin tildes, minúsculas)
_ALIAS_COLUMNAS = {
    "documento": {"numero de documento", "numero documento", "nro documento",
                  "no documento", "documento", "identificacion", "cedula"},
    "nombres": {"nombres", "nombre", "nombre(s)"},
    "apellidos": {"apellidos", "apellido"},
}
_ALIAS_CORREO = {"correo electronico", "correo", "e-mail", "email"}


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------
def _sin_tildes(texto: str) -> str:
    """'PEÑA' -> 'PENA', 'CÁRDENAS' -> 'CARDENAS'."""
    descompuesto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _normalizar_encabezado(valor) -> str:
    if valor is None:
        return ""
    texto = _sin_tildes(str(valor)).lower().strip()
    return re.sub(r"\s+", " ", texto)


def _normalizar_documento(valor) -> str:
    """Iguala documentos guardados como texto o número (123 vs '123.0')."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    if texto.endswith(".0"):
        texto = texto[:-2]
    return texto


def _tokens(texto) -> list[str]:
    """Palabras en minúscula, sin tildes ni símbolos."""
    if not texto:
        return []
    limpio = re.sub(r"[^a-z0-9 ]", " ", _sin_tildes(str(texto)).lower())
    return limpio.split()


def _partir_apellidos(texto) -> list[str]:
    """
    Separa los apellidos respetando las partículas:
    'DE LA CRUZ PEREZ' -> ['delacruz', 'perez'].
    """
    grupos, acumulado = [], []
    for token in _tokens(texto):
        acumulado.append(token)
        if token not in _PARTICULAS:
            grupos.append("".join(acumulado))
            acumulado = []
    if acumulado:
        grupos.append("".join(acumulado))
    return grupos


# ---------------------------------------------------------------------------
# Generación del correo
# ---------------------------------------------------------------------------
class _VariablesPlantilla(dict):
    def __missing__(self, clave):
        raise ValueError(
            f"La plantilla usa la variable desconocida {{{clave}}}. "
            "Disponibles: nombre1, nombre2, inicial, iniciales, apellido1, "
            "apellido2, doc, doc2, doc3, doc4."
        )


def _variables(nombres, apellidos, documento: str) -> dict:
    n = _tokens(nombres)
    a = _partir_apellidos(apellidos)
    return _VariablesPlantilla(
        nombre1=n[0] if n else "",
        nombre2=n[1] if len(n) > 1 else "",
        inicial=n[0][0] if n else "",
        iniciales="".join(t[0] for t in n),
        apellido1=a[0] if a else "",
        apellido2=a[1] if len(a) > 1 else "",
        doc=documento,
        doc2=documento[-2:],
        doc3=documento[-3:],
        doc4=documento[-4:],
    )


def generar_usuario(nombres, apellidos, documento, plantilla: str = PLANTILLA_DEFECTO) -> str:
    """Parte local del correo (lo que va antes de la @)."""
    usuario = plantilla.format_map(_variables(nombres, apellidos, _normalizar_documento(documento)))
    return re.sub(r"[^a-z0-9._-]", "", usuario.lower())


def generar_correo(nombres, apellidos, documento,
                   plantilla: str = PLANTILLA_DEFECTO,
                   dominio: str = DOMINIO_DEFECTO) -> str:
    usuario = generar_usuario(nombres, apellidos, documento, plantilla)
    return f"{usuario}@{dominio.lstrip('@')}" if usuario else ""


def validar_plantilla(plantilla: str) -> None:
    """Lanza ValueError si la plantilla es inválida (variable inexistente, llaves mal cerradas...)."""
    try:
        prueba = generar_usuario("Ana Maria", "de la Cruz Perez", "1234567890", plantilla)
    except (KeyError, IndexError, AttributeError, ValueError) as e:
        raise ValueError(f"Plantilla de correo inválida: {e}") from e
    if not prueba:
        raise ValueError("La plantilla no genera ningún texto.")


# ---------------------------------------------------------------------------
# Localización de columnas (sin posiciones fijas)
# ---------------------------------------------------------------------------
def _detectar_encabezado(ws, fila_encabezado: int | None = None, col_documento: int | None = None):
    """
    Devuelve (fila_encabezado, {'documento': col, 'nombres': col, 'apellidos': col}, col_correo|None)
    o None si la hoja no tiene la estructura esperada.

    - fila_encabezado / col_documento son opcionales: si se pasan, se respetan;
      si no, se detectan por el texto de las cabeceras.
    """
    filas = [fila_encabezado] if fila_encabezado else range(1, min(ws.max_row, MAX_FILAS_BUSQUEDA_ENCABEZADO) + 1)

    for fila in filas:
        encontradas: dict[str, int] = {}
        col_correo = None
        for col in range(1, ws.max_column + 1):
            texto = _normalizar_encabezado(ws.cell(fila, col).value)
            if not texto:
                continue
            for clave, alias in _ALIAS_COLUMNAS.items():
                if texto in alias and clave not in encontradas:
                    encontradas[clave] = col
            if texto in _ALIAS_CORREO and col_correo is None:
                col_correo = col

        if col_documento:
            encontradas["documento"] = col_documento

        if {"documento", "nombres", "apellidos"} <= encontradas.keys():
            return fila, encontradas, col_correo

    return None


def _ultima_columna_con_encabezado(ws, fila: int) -> int:
    for col in range(ws.max_column, 0, -1):
        if ws.cell(fila, col).value not in (None, ""):
            return col
    return 1


# ---------------------------------------------------------------------------
# Proceso principal
# ---------------------------------------------------------------------------
def generar_correos_consolidado(
    consolidado: Path,
    salida_dir: Path,
    plantilla: str = PLANTILLA_DEFECTO,
    dominio: str = DOMINIO_DEFECTO,
    fila_encabezado: int | None = None,
    col_documento: int | None = None,
) -> dict:
    """
    Agrega una columna "Correo Electrónico" a cada hoja del consolidado con el
    correo generado a partir de nombres, apellidos y documento.

    El consolidado original no se modifica: se guarda una copia en salida_dir.
    Cada documento recibe siempre el mismo correo aunque aparezca en muchas
    filas (una por resultado de aprendizaje). Si dos aprendices distintos
    generan el mismo correo, al segundo se le agrega un sufijo numérico y
    queda listado en "correos_con_colision" para que se revise.
    """
    validar_plantilla(plantilla)
    salida_dir.mkdir(parents=True, exist_ok=True)

    wb = cargar_workbook_compatible(consolidado)

    correo_por_documento: dict[str, str] = {}   # documento -> correo (global a todas las hojas)
    documento_por_correo: dict[str, str] = {}   # correo -> documento (para detectar colisiones)
    colisiones: list[dict] = []
    documentos_sin_datos: list[str] = []
    hojas_omitidas: list[str] = []

    total_filas = 0
    filas_con_correo = 0

    for ws in wb.worksheets:
        ubicacion = _detectar_encabezado(ws, fila_encabezado, col_documento)
        if ubicacion is None:
            hojas_omitidas.append(ws.title)
            continue

        fila_enc, cols, col_correo = ubicacion

        if col_correo is None:
            col_correo = _ultima_columna_con_encabezado(ws, fila_enc) + 1
            celda_titulo = ws.cell(fila_enc, col_correo, TITULO_COLUMNA_CORREO)
            # Mismo estilo que el resto de encabezados de la hoja
            referencia = ws.cell(fila_enc, col_correo - 1)
            if referencia.has_style:
                celda_titulo._style = copy(referencia._style)
            ws.column_dimensions[get_column_letter(col_correo)].width = 34

        for fila in range(fila_enc + 1, ws.max_row + 1):
            documento = _normalizar_documento(ws.cell(fila, cols["documento"]).value)
            if not documento:
                continue

            total_filas += 1

            if documento not in correo_por_documento:
                nombres = ws.cell(fila, cols["nombres"]).value
                apellidos = ws.cell(fila, cols["apellidos"]).value
                correo = generar_correo(nombres, apellidos, documento, plantilla, dominio)

                if not correo:
                    documentos_sin_datos.append(documento)
                    correo_por_documento[documento] = ""
                else:
                    if correo in documento_por_correo:
                        original = correo
                        usuario, _, dom = correo.partition("@")
                        n = 2
                        while f"{usuario}{n}@{dom}" in documento_por_correo:
                            n += 1
                        correo = f"{usuario}{n}@{dom}"
                        colisiones.append({
                            "correo_original": original,
                            "documento_previo": documento_por_correo[original],
                            "documento_nuevo": documento,
                            "correo_asignado": correo,
                        })
                    documento_por_correo[correo] = documento
                    correo_por_documento[documento] = correo

            correo = correo_por_documento[documento]
            if correo:
                ws.cell(fila, col_correo, correo)
                filas_con_correo += 1

    if len(hojas_omitidas) == len(wb.worksheets):
        raise ValueError(
            "No se encontraron las columnas 'Número de Documento', 'Nombres' y 'Apellidos' "
            "en ninguna hoja del consolidado."
        )

    ruta_salida = salida_dir / f"{Path(consolidado).stem}_ConCorreos.xlsx"
    wb.save(ruta_salida)

    return {
        "archivo_generado": str(ruta_salida),
        "plantilla_usada": f"{plantilla}@{dominio.lstrip('@')}",
        "total_aprendices": len(correo_por_documento),
        "total_correos_generados": len(documento_por_correo),
        "total_documentos_consolidado": total_filas,      # filas con documento
        "total_encontrados": filas_con_correo,            # filas que recibieron correo
        "total_sin_correo": total_filas - filas_con_correo,
        "documentos_sin_correo": documentos_sin_datos[:50],
        "correos_con_colision": colisiones,
        "hojas_omitidas": hojas_omitidas,
    }