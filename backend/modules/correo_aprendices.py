"""
modules/correo_aprendices.py

Cruza el número de documento del consolidado (columna configurable, por
defecto D) contra varios archivos .xls/.xlsx/.xlsm (columna B = documento,
columna F = correo, filas configurables) y escribe el correo encontrado
en una columna nueva al final de cada hoja del consolidado.
"""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from modules.file_utils import cargar_workbook_compatible


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


def construir_mapa_documento_correo(archivos_xls: list[Path], fila_inicio: int) -> tuple[dict, dict]:
    """
    Recorre todas las hojas de todos los archivos y arma:
    - mapa: documento normalizado -> correo (primer valor encontrado)
    - duplicados: documento -> lista de correos distintos encontrados
      (para poder avisar si el mismo documento aparece con más de un
      correo en los archivos fuente, en vez de quedarnos callados con
      cuál se usó)
    """
    mapa: dict[str, str] = {}
    duplicados: dict[str, list[str]] = {}

    for archivo in archivos_xls:
        wb = cargar_workbook_compatible(archivo)
        for ws in wb.worksheets:
            for fila in range(fila_inicio, ws.max_row + 1):
                documento = _normalizar_documento(ws.cell(fila, 2).value)   # Columna B
                correo_valor = ws.cell(fila, 6).value                       # Columna F
                correo = str(correo_valor).strip() if correo_valor is not None else ""

                if not documento or not correo:
                    continue

                if documento not in mapa:
                    mapa[documento] = correo
                elif mapa[documento] != correo:
                    duplicados.setdefault(documento, [mapa[documento]])
                    if correo not in duplicados[documento]:
                        duplicados[documento].append(correo)

    return mapa, duplicados


def enriquecer_con_correos(
    consolidado: Path,
    archivos_xls: list[Path],
    salida_dir: Path,
    fila_inicio_consolidado: int = 2,
    col_documento_consolidado: int = 4,   # Columna D
    fila_inicio_xls: int = 2,
) -> dict:
    """
    Devuelve estadísticas + ruta del archivo generado. El consolidado
    original no se modifica: se guarda una copia procesada en salida_dir.
    """
    salida_dir.mkdir(parents=True, exist_ok=True)

    mapa_correos, duplicados = construir_mapa_documento_correo(archivos_xls, fila_inicio_xls)

    wb = cargar_workbook_compatible(consolidado)

    total_documentos = 0
    total_encontrados = 0
    documentos_sin_correo = []

    for ws in wb.worksheets:
        col_salida = ws.max_column + 1
        ws.cell(fila_inicio_consolidado - 1, col_salida, "Correo Electrónico").font = Font(bold=True)

        for fila in range(fila_inicio_consolidado, ws.max_row + 1):
            documento_valor = ws.cell(fila, col_documento_consolidado).value
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
    }