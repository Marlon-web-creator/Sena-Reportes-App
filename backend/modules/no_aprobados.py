"""
modules/no_aprobados.py

Lógica de consolidación de "Reporte Juicios de Evaluación" (uno por ficha),
adaptada del script original NoAprobados.py para ser invocada desde la API
en lugar de por consola. La lógica de negocio (columnas, programas, filtros)
es exactamente la misma que en el script original.
"""

import re
import unicodedata
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

# --------------------------------------------------------------------------
# CONFIGURACIÓN (idéntica al script original)
# --------------------------------------------------------------------------

FILA_FICHA = 2
FILA_CODIGO = 3
FILA_DENOMINACION = 5
COL_VALOR_ENCABEZADO = 2

FILTROS = {
    "NO APROBADO": {"valor": "NO APROBADO", "etiqueta": "no aprobados", "sufijo": "NoAprobados"},
    "POR EVALUAR": {"valor": "POR EVALUAR", "etiqueta": "por evaluar", "sufijo": "PorEvaluar"},
}

ESTADOS_POR_EVALUAR = {"EN FORMACION", "CONDICIONADO", "CONDICIONADOS"}

# Columnas que se extraen de la tabla de detalle, identificadas por el
# TEXTO de su encabezado (normalizado) en vez de por posición fija.
# Así, si el reporte de SENA cambia el orden de las columnas o inserta
# alguna nueva (como la columna vacía que aparece hoy entre "Juicio de
# Evaluación" y "Fecha y Hora..."), la extracción sigue funcionando.
# Formato: (encabezado_en_el_archivo, nombre_de_columna_en_la_salida)
COLUMNAS_DESEADAS = [
    ("Tipo de Documento", "Tipo de Documento"),
    ("Número de Documento", "Número de Documento"),
    ("Nombre", "Nombres"),
    ("Apellidos", "Apellidos"),
    ("Estado", "Estado"),
    ("Competencia", "Competencia"),
    ("Resultado de Aprendizaje", "Resultado de Aprendizaje"),
    ("Juicio de Evaluación", "Juicio de Evaluación"),
    ("Funcionario que registro el juicio evaluativo", "Funcionario que registró el juicio evaluativo"),
]

# Encabezados usados para filtrar filas (deben existir en COLUMNAS_DESEADAS
# o poder ubicarse igualmente por nombre).
ENCABEZADO_JUICIO = "Juicio de Evaluación"
ENCABEZADO_ESTADO = "Estado"

ENCABEZADOS_SALIDA = ["Código", "Programa"] + [salida for _, salida in COLUMNAS_DESEADAS]

RAMOS = [
    "DESARROLLO CREATIVO DE PRODUCTOS PARA LA INDUSTRIA",
    "DESARROLLO Y ADAPTACION DE PROTESIS Y ORTESIS",
    "DISEÑO E INTEGRACIÓN DE AUTOMATISMOS MECATRÓNICOS",
    "DESARROLLO DE COMPONENTES MECANICOS",
    "DIBUJO MECANICO",
]

GELVES = [
    "ANALISIS Y DESARROLLO DE SOFTWARE",
    "PROGRAMACION DE SOFTWARE",
    "ASEGURAMIENTO METROLOGICO INDUSTRIAL",
    "CONTROL DE LA SEGURIDAD DIGITAL",
    "MODELADO DIGITAL DE PRODUCTOS INDUSTRIALES",
    "MEDICIONES FISICAS",
    "TRATAMIENTO DE RIESGOS DE CIBERSEGURIDAD EN LA MICRO, PEQUEÑA Y MEDIANA EMPRESA (MIPYMES).",
]

SIGLAS = {
    "DESARROLLO CREATIVO DE PRODUCTOS PARA LA INDUSTRIA": "DCPI",
    "DESARROLLO Y ADAPTACION DE PROTESIS Y ORTESIS": "DAPO",
    "DISEÑO E INTEGRACIÓN DE AUTOMATISMOS MECATRÓNICOS": "DIAM",
    "DESARROLLO DE COMPONENTES MECANICOS": "DCM",
    "DIBUJO MECANICO": "DM",
    "ANALISIS Y DESARROLLO DE SOFTWARE": "ADSO",
    "PROGRAMACION DE SOFTWARE": "PS",
    "ASEGURAMIENTO METROLOGICO INDUSTRIAL": "AMI",
    "CONTROL DE LA SEGURIDAD DIGITAL": "CSD",
}


def normalizar(texto) -> str:
    if texto is None:
        return ""
    texto = str(texto).replace("\xa0", " ").replace(".", " ")
    texto = re.sub(r"\s+", " ", texto).strip().upper()
    texto = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return texto


RAMOS_NORM = {normalizar(p) for p in RAMOS}
GELVES_NORM = {normalizar(p) for p in GELVES}
SIGLAS_NORM = {normalizar(k): v for k, v in SIGLAS.items()}
ESTADOS_POR_EVALUAR_NORM = {normalizar(e) for e in ESTADOS_POR_EVALUAR}


def leer_valor_encabezado(df: pd.DataFrame, fila: int, col: int = COL_VALOR_ENCABEZADO):
    try:
        return df.iat[fila, col]
    except IndexError:
        return None


def formatear_ficha(valor) -> str:
    if valor is None:
        return "SIN_FICHA"
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def encontrar_fila_encabezado_tabla(df: pd.DataFrame) -> int:
    for i in range(df.shape[0]):
        valor = df.iat[i, 0]
        if isinstance(valor, str) and valor.strip().lower() == "tipo de documento":
            return i
    raise ValueError("No se encontró la fila de encabezado de la tabla ('Tipo de Documento').")


def mapear_columnas(df: pd.DataFrame, fila_encabezado: int) -> dict:
    """
    Recorre la fila de encabezados de la tabla de detalle y arma un
    diccionario {encabezado_normalizado: índice_de_columna}.

    Esto es lo que hace "procedural" la extracción: en vez de asumir que
    'Competencia' siempre está en la columna 5 y 'Juicio de Evaluación' en
    la 7, se busca cada encabezado por su texto. Si el reporte agrega,
    quita o reordena columnas, la extracción se sigue ubicando sola.
    """
    fila = df.iloc[fila_encabezado]
    mapa = {}
    for idx, valor in enumerate(fila):
        nombre = normalizar(valor)
        if nombre and nombre not in mapa:  # conserva la primera aparición
            mapa[nombre] = idx
    return mapa


def resolver_indices_columnas(mapa_columnas: dict) -> tuple[list[int], int, int]:
    """
    A partir del mapa {encabezado_normalizado: índice}, resuelve:
    - la lista de índices en el orden de COLUMNAS_DESEADAS
    - el índice de la columna de 'Juicio de Evaluación' (para filtrar)
    - el índice de la columna de 'Estado' (para el filtro POR EVALUAR)

    Lanza ValueError con un mensaje claro si falta alguna columna esperada,
    en vez de fallar silenciosamente o leer datos de la columna equivocada.
    """
    faltantes = [
        encabezado for encabezado, _ in COLUMNAS_DESEADAS
        if normalizar(encabezado) not in mapa_columnas
    ]
    if normalizar(ENCABEZADO_JUICIO) not in mapa_columnas:
        faltantes.append(ENCABEZADO_JUICIO)
    if normalizar(ENCABEZADO_ESTADO) not in mapa_columnas:
        faltantes.append(ENCABEZADO_ESTADO)
    if faltantes:
        faltantes_unicos = list(dict.fromkeys(faltantes))
        raise ValueError(
            "No se encontraron en el archivo las columnas esperadas: "
            + ", ".join(faltantes_unicos)
        )

    idx_columnas = [mapa_columnas[normalizar(encabezado)] for encabezado, _ in COLUMNAS_DESEADAS]
    idx_juicio = mapa_columnas[normalizar(ENCABEZADO_JUICIO)]
    idx_estado = mapa_columnas[normalizar(ENCABEZADO_ESTADO)]
    return idx_columnas, idx_juicio, idx_estado


def procesar_archivo(path: Path, valor_filtro: str) -> dict:
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
    df = pd.read_excel(path, header=None, engine=engine)

    ficha = formatear_ficha(leer_valor_encabezado(df, FILA_FICHA))
    codigo = leer_valor_encabezado(df, FILA_CODIGO)
    denominacion = leer_valor_encabezado(df, FILA_DENOMINACION)
    denom_norm = normalizar(denominacion)

    carpeta = "RAMOS" if denom_norm in RAMOS_NORM else "GELVES" if denom_norm in GELVES_NORM else None
    siglas = SIGLAS_NORM.get(denom_norm, str(denominacion))

    filas_salida = []
    if carpeta is not None:
        fila_encabezado = encontrar_fila_encabezado_tabla(df)
        mapa_columnas = mapear_columnas(df, fila_encabezado)
        idx_columnas, idx_juicio, idx_estado = resolver_indices_columnas(mapa_columnas)

        tabla = df.iloc[fila_encabezado + 1:].reset_index(drop=True)
        col_juicio = tabla.iloc[:, idx_juicio].astype(str).str.strip().str.upper()
        coincidencias = tabla[col_juicio == valor_filtro]

        if valor_filtro == "POR EVALUAR":
            col_estado = coincidencias.iloc[:, idx_estado].apply(normalizar)
            coincidencias = coincidencias[col_estado.isin(ESTADOS_POR_EVALUAR_NORM)]

        for _, fila in coincidencias.iterrows():
            valores = [fila.iloc[c] if c < len(fila) else None for c in idx_columnas]
            filas_salida.append([codigo, denominacion] + valores)

    return {
        "ficha": ficha, "codigo": codigo, "denominacion": denominacion,
        "siglas": siglas, "carpeta": carpeta, "filas": filas_salida,
    }


def escribir_hoja(wb: Workbook, ficha: str, programa: str, filas: list, filtro: dict):
    nombre_hoja = re.sub(r"[\[\]:\*\?/\\]", "_", ficha)[:31]

    nombre_original = nombre_hoja
    contador = 1
    while nombre_hoja in wb.sheetnames:
        sufijo = f"_{contador}"
        nombre_hoja = nombre_original[: 31 - len(sufijo)] + sufijo
        contador += 1

    ws = wb.create_sheet(title=nombre_hoja)
    n_col = len(ENCABEZADOS_SALIDA)
    etiqueta = filtro["etiqueta"]

    if filtro["valor"] == "NO APROBADO":
        ws.sheet_properties.tabColor = "FF0000" if filas else "00B050"

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_col)
    ws.cell(row=1, column=1, value=f"FICHA: {ficha}").font = Font(bold=True, size=13)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n_col)
    ws.cell(row=2, column=1, value=f"PROGRAMA: {programa}").font = Font(bold=True, size=11)

    for c in range(1, n_col + 1):
        ws.column_dimensions[get_column_letter(c)].width = 22

    if not filas:
        ws.merge_cells(start_row=4, start_column=1, end_row=4, end_column=n_col)
        ws.cell(row=4, column=1,
                value=f"No hay juicios {etiqueta} para la ficha: {ficha}").font = Font(italic=True)
        return ws

    fila_tabla = 4
    for c, encabezado in enumerate(ENCABEZADOS_SALIDA, start=1):
        ws.cell(row=fila_tabla, column=c, value=encabezado)
    for r, fila in enumerate(filas, start=fila_tabla + 1):
        for c, valor in enumerate(fila, start=1):
            ws.cell(row=r, column=c, value=valor)
    fila_fin = fila_tabla + len(filas)

    ref = f"A{fila_tabla}:{get_column_letter(n_col)}{fila_fin}"
    nombre_tabla = "T_" + re.sub(r"\W", "_", nombre_hoja)
    tabla = Table(displayName=nombre_tabla, ref=ref)
    tabla.tableStyleInfo = TableStyleInfo(name="TableStyleMedium11", showRowStripes=True)
    ws.add_table(tabla)

    fila_total = fila_fin + 2
    ws.cell(row=fila_total, column=1, value=f"Total de {etiqueta.upper()}:").font = Font(bold=True)
    ws.cell(row=fila_total, column=2, value=len(filas)).font = Font(bold=True)

    return ws


def consolidar(
    archivos: list[Path],
    valor_filtro: str,
    generar_general: bool,
    salida_dir: Path,
) -> dict:
    """
    Procesa una lista de archivos de reporte y genera los libros Excel
    consolidados (Ramos, Gelves y opcionalmente General) en salida_dir.

    Devuelve un dict con las estadísticas y las rutas de los archivos
    generados, listo para ser serializado como respuesta JSON.
    """
    if valor_filtro not in FILTROS:
        raise ValueError(f"Filtro no reconocido: {valor_filtro}")
    filtro = FILTROS[valor_filtro]

    salida_dir.mkdir(parents=True, exist_ok=True)

    libros = {"RAMOS": Workbook(), "GELVES": Workbook()}
    for wb in libros.values():
        wb.remove(wb.active)

    wb_general = None
    if generar_general:
        wb_general = Workbook()
        wb_general.remove(wb_general.active)

    stats = {
        "fichas": {"RAMOS": 0, "GELVES": 0},
        "coincidencias": {"RAMOS": 0, "GELVES": 0},
        "no_reconocidos": [],
        "errores": [],
        "detalle_fichas": [],
    }

    for path in archivos:
        try:
            r = procesar_archivo(path, filtro["valor"])
        except Exception as e:
            stats["errores"].append({"archivo": path.name, "error": str(e)})
            continue

        if r["carpeta"] is None:
            stats["no_reconocidos"].append({
                "archivo": path.name, "ficha": r["ficha"], "denominacion": r["denominacion"],
            })
            continue

        escribir_hoja(libros[r["carpeta"]], r["ficha"], r["denominacion"], r["filas"], filtro)
        stats["fichas"][r["carpeta"]] += 1
        stats["coincidencias"][r["carpeta"]] += len(r["filas"])
        stats["detalle_fichas"].append({
            "archivo": path.name, "ficha": r["ficha"], "programa": r["siglas"],
            "carpeta": r["carpeta"], "coincidencias": len(r["filas"]),
        })
        if generar_general:
            escribir_hoja(wb_general, r["ficha"], r["denominacion"], r["filas"], filtro)

    sufijo = filtro["sufijo"]
    archivos_generados = {}

    ruta_ramos = salida_dir / f"Consolidado_Ramos_{sufijo}.xlsx"
    ruta_gelves = salida_dir / f"Consolidado_Gelves_{sufijo}.xlsx"
    if libros["RAMOS"].sheetnames:
        libros["RAMOS"].save(ruta_ramos)
        archivos_generados["RAMOS"] = ruta_ramos
    if libros["GELVES"].sheetnames:
        libros["GELVES"].save(ruta_gelves)
        archivos_generados["GELVES"] = ruta_gelves

    if generar_general and wb_general.sheetnames:
        ruta_general = salida_dir / f"Consolidado_General_{sufijo}.xlsx"
        wb_general.save(ruta_general)
        archivos_generados["GENERAL"] = ruta_general

    return {
        "filtro": filtro["valor"],
        "archivos_procesados": len(archivos),
        "stats": stats,
        "archivos_generados": {k: str(v) for k, v in archivos_generados.items()},
    }