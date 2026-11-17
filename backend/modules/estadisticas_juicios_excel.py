"""
modules/estadisticas_juicios_excel.py

Genera el Excel descargable del módulo "Estadísticas y proyección de
juicios" a partir del resultado de estadisticas_juicios.analizar().

Hojas: Resumen, Proyección, Por programa, Distribuciones, Fichas,
Aprendices, RAPs, Instructores.

La hoja "Proyección" usa FÓRMULAS y celdas de supuestos editables
(azul sobre amarillo): al cambiar una tasa en Excel, los resultados se
recalculan solos. Los listados (Fichas, Aprendices, RAPs, Instructores)
van COMPLETOS, sin top.
"""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .estadisticas_juicios import CATEGORIAS

FUENTE = "Arial"
VERDE = "39A900"
F_TITULO = Font(name=FUENTE, size=14, bold=True, color=VERDE)
F_SUB = Font(name=FUENTE, size=10, italic=True, color="595959")
F_HEAD = Font(name=FUENTE, size=10, bold=True, color="FFFFFF")
F_BASE = Font(name=FUENTE, size=10)
F_NEG = Font(name=FUENTE, size=10, bold=True)
F_INPUT = Font(name=FUENTE, size=10, color="0000FF")          # supuestos / datos fijos
FILL_HEAD = PatternFill("solid", fgColor=VERDE)
FILL_INPUT = PatternFill("solid", fgColor="FFFF99")
FILL_RES = PatternFill("solid", fgColor="E8F5E0")
BORDE = Border(bottom=Side(style="thin", color="D9D9D9"))

FMT_INT = "#,##0"
FMT_PCT = "0.0%"
FMT_DEC = "#,##0.0"


# ------------------------------------------------------------------ helpers
def _celda(ws, ref, valor, fuente=F_BASE, fmt=None, fill=None, align=None):
    c = ws[ref]
    c.value = valor
    c.font = fuente
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = fill
    if align:
        c.alignment = align
    return c


def _titulo(ws, texto, sub=None):
    _celda(ws, "A1", texto, F_TITULO)
    if sub:
        _celda(ws, "A2", sub, F_SUB)


def _encabezado(ws, fila, columnas, col_ini=1):
    for i, texto in enumerate(columnas):
        c = ws.cell(fila, col_ini + i, texto)
        c.font, c.fill = F_HEAD, FILL_HEAD
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _tabla(ws, fila_ini, columnas, filas, formatos=None, col_ini=1, filtro=False, congelar=False):
    """
    columnas: [(titulo, clave)], filas: lista de dicts.
    Devuelve la siguiente fila libre.
    """
    formatos = formatos or {}
    _encabezado(ws, fila_ini, [t for t, _ in columnas], col_ini)
    for i, fila in enumerate(filas, start=fila_ini + 1):
        for j, (_, clave) in enumerate(columnas):
            c = ws.cell(i, col_ini + j, fila.get(clave))
            c.font, c.border = F_BASE, BORDE
            if clave in formatos:
                c.number_format = formatos[clave]
    ultima = fila_ini + len(filas)
    if filtro and filas:
        ws.auto_filter.ref = (
            f"{get_column_letter(col_ini)}{fila_ini}:"
            f"{get_column_letter(col_ini + len(columnas) - 1)}{ultima}"
        )
    if congelar:
        ws.freeze_panes = ws.cell(fila_ini + 1, col_ini)
    return ultima + 2


def _anchos(ws, anchos: dict):
    for col, w in anchos.items():
        ws.column_dimensions[col].width = w


def _pct(lista):
    """Convierte 'pct' (0-100) a fracción para formato de porcentaje."""
    return [{**f, "pct": (f.get("pct") or 0) / 100} for f in lista]


# -------------------------------------------------------------------- hojas
def _hoja_resumen(wb, res):
    ws = wb.active
    ws.title = "Resumen"
    s = res["estadisticas"]
    r = s["resumen"]
    _titulo(ws, "Estadísticas de juicios pendientes",
            f"Reporte del {r['fecha_reporte']} · Archivo SVP: {r['archivo_origen'] or '—'}")

    kpis = [
        ("Total de juicios pendientes", r["total_pendientes"], FMT_INT),
        ("Aprendices con juicios pendientes", r["aprendices"], FMT_INT),
        ("Fichas", r["fichas"], FMT_INT),
        ("Programas de formación", r["programas"], FMT_INT),
        ("Resultados de aprendizaje (RAP) distintos", r["raps_distintos"], FMT_INT),
        ("Instructores SVP", r["instructores_svp"], FMT_INT),
        ("Instructores Synchronize", r["instructores_sync"], FMT_INT),
        ("Promedio de juicios pendientes por aprendiz", r["promedio_por_aprendiz"], FMT_DEC),
        ("Mediana de juicios pendientes por aprendiz", r["mediana_por_aprendiz"], FMT_DEC),
        ("Máximo de juicios pendientes en un aprendiz", r["maximo_por_aprendiz"], FMT_INT),
    ]
    _encabezado(ws, 4, ["Indicador", "Valor"])
    for i, (nombre, valor, fmt) in enumerate(kpis, start=5):
        _celda(ws, f"A{i}", nombre)
        _celda(ws, f"B{i}", valor, F_NEG, fmt)
        ws[f"A{i}"].border = ws[f"B{i}"].border = BORDE

    fila = 5 + len(kpis) + 1
    _celda(ws, f"A{fila}", "Estado de los juicios pendientes", F_NEG)
    gestion = [{"descripcion": g["descripcion"], "pendientes": g["pendientes"], "pct": g["pct"] / 100}
               for g in s["gestion"]]
    fila = _tabla(ws, fila + 1, [("Tipo", "descripcion"), ("Juicios pendientes", "pendientes"), ("%", "pct")],
                  gestion, {"pendientes": FMT_INT, "pct": FMT_PCT})

    rc = s["riesgo_cierre"]
    _celda(ws, f"A{fila}", "Riesgo de cierre", F_NEG)
    _celda(ws, f"A{fila + 1}", f"Juicios pendientes en fichas de {rc['trimestre'] or '—'} (trimestre más alto observado)")
    _celda(ws, f"B{fila + 1}", rc["pendientes"], F_NEG, FMT_INT)
    _celda(ws, f"C{fila + 1}", rc["pct"] / 100, F_NEG, FMT_PCT)

    ob = s.get("raps_por_trimestre_observado") or {}
    if ob.get("promedio") is not None:
        fila += 3
        _celda(ws, f"A{fila}", "RAPs (resultados de aprendizaje) que se ven por trimestre, observado en el archivo", F_NEG)
        _celda(ws, f"A{fila + 1}", "Promedio por ficha y trimestre")
        _celda(ws, f"B{fila + 1}", ob["promedio"], F_NEG, FMT_DEC)
        _celda(ws, f"A{fila + 2}", "Mediana")
        _celda(ws, f"B{fila + 2}", ob["mediana"], F_NEG, FMT_DEC)
        _celda(ws, f"A{fila + 3}", ob.get("nota", ""), F_SUB)
    _anchos(ws, {"A": 72, "B": 18, "C": 14})


def _hoja_proyeccion(wb, res):
    ws = wb.create_sheet("Proyección")
    p = res["proyeccion"]
    e = p["estructural"]
    esc = e["escenarios"]
    orden = [n for n in ("conservador", "base", "optimista") if n in esc]
    cols = ["C", "D", "E"][: len(orden)]

    _titulo(ws, f"Proyección de juicios pendientes para {p['trimestre_proyectado']}",
            f"Parte de {p['trimestre_actual']}. Celdas azules sobre amarillo = supuestos editables; el resto son fórmulas.")

    # --- datos de partida
    _encabezado(ws, 4, ["Dato de partida", "Valor"])
    datos = [
        (5, "Juicios pendientes actuales", e["base_pendientes"], FMT_INT),
        (6, "Aprendices con juicios pendientes", e["aprendices"], FMT_INT),
        (7, "RAPs (resultados de aprendizaje) que ve cada aprendiz por trimestre", e["raps_por_trimestre"], FMT_INT),
    ]
    for fila, nombre, valor, fmt in datos:
        _celda(ws, f"A{fila}", nombre)
        _celda(ws, f"B{fila}", valor, F_INPUT, fmt, FILL_INPUT if fila == 7 else None)

    # --- supuestos por escenario
    _encabezado(ws, 9, ["Supuestos por escenario", "Juicios pendientes actuales"] + [n.capitalize() for n in orden])
    for k, (clave, desc) in enumerate(CATEGORIAS.items()):
        fila = 10 + k
        _celda(ws, f"A{fila}", f"% que se califica en el trimestre: {desc}")
        _celda(ws, f"B{fila}", e["categorias"][clave], F_INPUT, FMT_INT)
        for col, nombre in zip(cols, orden):
            _celda(ws, f"{col}{fila}", esc[nombre]["supuestos"][clave], F_INPUT, FMT_PCT, FILL_INPUT)
    _celda(ws, "A13", "% de juicios nuevos que se califican dentro del mismo trimestre")
    for col, nombre in zip(cols, orden):
        _celda(ws, f"{col}13", esc[nombre]["supuestos"]["calif_oportuna"], F_INPUT, FMT_PCT, FILL_INPUT)

    # --- resultados (fórmulas)
    _encabezado(ws, 15, ["Resultado"] + [""] + [n.capitalize() for n in orden])
    filas = [
        (16, "Juicios pendientes actuales", "=$B$5", FMT_INT),
        (17, "(−) Juicios pendientes que se califican en el trimestre", "=SUMPRODUCT($B$10:$B$12,{c}10:{c}12)", FMT_INT),
        (18, "Juicios nuevos del trimestre (aprendices × RAPs por trimestre)", "=$B$6*$B$7", FMT_INT),
        (19, "(+) Juicios nuevos que quedan sin calificar", "={c}18*(1-{c}13)", FMT_INT),
        (20, "Juicios pendientes proyectados al final del trimestre", "={c}16-{c}17+{c}19", FMT_INT),
        (21, "Reducción neta (cantidad de juicios)", "={c}16-{c}20", FMT_INT),
        (22, "Reducción neta (%)", "=IF({c}16=0,0,{c}21/{c}16)", FMT_PCT),
        (23, "Promedio proyectado de juicios pendientes por aprendiz", "=IF($B$6=0,0,{c}20/$B$6)", FMT_DEC),
    ]
    for fila, nombre, formula, fmt in filas:
        destacada = fila in (20, 22)
        _celda(ws, f"A{fila}", nombre, F_NEG if destacada else F_BASE)
        for col in cols:
            _celda(ws, f"{col}{fila}", formula.format(c=col), F_NEG if destacada else F_BASE, fmt,
                   FILL_RES if destacada else None)
        ws[f"A{fila}"].border = BORDE

    # --- método y advertencia
    notas = [
        "Cómo se calcula: juicios proyectados = juicios pendientes actuales − juicios que se califican + juicios nuevos que quedan sin calificar.",
        "Juicios que se califican = suma de (juicios pendientes de cada tipo × % que se califica en el trimestre).",
        "Juicios nuevos que quedan sin calificar = aprendices × RAPs por trimestre × (1 − % que se califica a tiempo).",
        "RAP = resultado de aprendizaje. Cada RAP que ve un aprendiz genera un juicio que debe calificarse.",
        e["advertencia"],
        "No se descuenta a los aprendices que terminan su etapa lectiva ni se suman los que ingresan.",
    ]
    for i, t in enumerate(notas, start=25):
        _celda(ws, f"A{i}", t, F_SUB)

    # --- tendencia histórica
    h = p["historica"]
    fila = 25 + len(notas) + 1
    _celda(ws, f"A{fila}", "Tendencia histórica (reportes anteriores)", F_NEG)
    if h.get("disponible"):
        _celda(ws, f"A{fila + 1}", f"Juicios pendientes proyectados al {h['fecha_proyeccion']}")
        _celda(ws, f"B{fila + 1}", h["pendientes_proyectados"], F_NEG, FMT_INT)
        _celda(ws, f"A{fila + 2}", "Cambio promedio de juicios pendientes por día")
        _celda(ws, f"B{fila + 2}", h["cambio_por_dia"], F_NEG, FMT_DEC)
        _celda(ws, f"A{fila + 3}", f"Reportes usados: {h['puntos']} · confiabilidad {h['confiabilidad']}", F_SUB)
        _tabla(ws, fila + 5, [("Fecha", "fecha"), ("Juicios pendientes", "total_pendientes")],
               h["serie"], {"total_pendientes": FMT_INT})
    else:
        _celda(ws, f"A{fila + 1}", h["mensaje"], F_SUB)

    _anchos(ws, {"A": 105, "B": 22, "C": 16, "D": 16, "E": 16})
    ws.freeze_panes = "A4"


def _hoja_programas(wb, res):
    ws = wb.create_sheet("Por programa")
    _titulo(ws, "Juicios pendientes por programa de formación")
    _tabla(ws, 3, [("Programa", "programa"), ("Juicios pendientes", "pendientes"), ("% del total", "pct"),
                   ("Aprendices", "aprendices"), ("Fichas", "fichas"),
                   ("Juicios pendientes por aprendiz", "promedio_por_aprendiz"),
                   ("Sin instructor asignado", "sin_asignacion")],
           _pct(res["estadisticas"]["por_programa"]),
           {"pendientes": FMT_INT, "pct": FMT_PCT, "aprendices": FMT_INT, "fichas": FMT_INT,
            "promedio_por_aprendiz": FMT_DEC, "sin_asignacion": FMT_INT},
           filtro=True, congelar=True)
    _anchos(ws, {"A": 70, "B": 14, "C": 12, "D": 12, "E": 10, "F": 16, "G": 18})


def _hoja_distribuciones(wb, res):
    ws = wb.create_sheet("Distribuciones")
    s = res["estadisticas"]
    _titulo(ws, "Distribución de los juicios pendientes")
    bloques = [
        ("Por trimestre actual de la ficha", "por_trimestre_ficha"),
        ("Trimestre en que vieron el resultado", "por_trimestre_visto"),
        ("Trimestre del año programado en Synchronize", "por_trimestre_programacion_sync"),
        ("Por tipo (parcial / completo)", "por_tipo"),
        ("Por estado del aprendiz", "por_estado_aprendiz"),
    ]
    fila = 3
    for titulo, clave in bloques:
        _celda(ws, f"A{fila}", titulo, F_NEG)
        fila = _tabla(ws, fila + 1, [("Etiqueta", "etiqueta"), ("Juicios pendientes", "pendientes"), ("%", "pct")],
                      _pct(s[clave]), {"pendientes": FMT_INT, "pct": FMT_PCT})
    _anchos(ws, {"A": 44, "B": 14, "C": 10})


def _hoja_lista(wb, nombre, titulo, columnas, filas, formatos, anchos):
    ws = wb.create_sheet(nombre)
    _titulo(ws, titulo, f"{len(filas):,} registros (listado completo)".replace(",", "."))
    _tabla(ws, 4, columnas, filas, formatos, filtro=True, congelar=True)
    _anchos(ws, anchos)


def _hoja_instructores(wb, res):
    ws = wb.create_sheet("Instructores")
    c = res["_completo"]
    _titulo(ws, "Juicios pendientes por instructor (listado completo)")
    _celda(ws, "A3", "Instructor programado en SVP (juicios pendientes)", F_NEG)
    _celda(ws, "D3", "Instructor en Synchronize (en formación sin calificar)", F_NEG)
    _tabla(ws, 4, [("Instructor", "nombre"), ("Juicios pendientes", "pendientes")], c["instructores_svp"],
           {"pendientes": FMT_INT}, col_ini=1)
    _tabla(ws, 4, [("Instructor", "nombre"), ("Juicios pendientes", "pendientes")], c["instructores_sync"],
           {"pendientes": FMT_INT}, col_ini=4)
    ws.freeze_panes = "A5"
    _anchos(ws, {"A": 44, "B": 12, "C": 3, "D": 44, "E": 12})


# ---------------------------------------------------------------- principal
def generar_excel(resultado: dict, ruta: Path) -> Path:
    """resultado: salida de analizar() (incluye la clave '_completo')."""
    wb = Workbook()
    _hoja_resumen(wb, resultado)
    _hoja_proyeccion(wb, resultado)
    _hoja_programas(wb, resultado)
    _hoja_distribuciones(wb, resultado)

    c = resultado["_completo"]
    _hoja_lista(wb, "Fichas", "Juicios pendientes por ficha",
                [("Ficha", "ficha"), ("Programa", "programa"), ("Trimestre actual", "trim_actual"),
                 ("Aprendices", "aprendices"), ("Juicios pendientes", "pendientes")],
                c["fichas"], {"aprendices": FMT_INT, "pendientes": FMT_INT},
                {"A": 12, "B": 70, "C": 16, "D": 12, "E": 12})
    _hoja_lista(wb, "Aprendices", "Juicios pendientes por aprendiz",
                [("Documento", "documento"), ("Nombre", "nombre"), ("Ficha", "ficha"), ("Programa", "programa"),
                 ("Juicios pendientes", "pendientes"), ("Ya vistos (Synchronize)", "con_sync"),
                 ("Solo SVP", "solo_svp"), ("Sin instructor", "sin_asignacion")],
                c["aprendices"],
                {k: FMT_INT for k in ("pendientes", "con_sync", "solo_svp", "sin_asignacion")},
                {"A": 14, "B": 38, "C": 12, "D": 60, "E": 12, "F": 14, "G": 10, "H": 14})
    _hoja_lista(wb, "RAPs", "Juicios pendientes por resultado de aprendizaje",
                [("ID RAP", "id_rap"), ("Resultado de aprendizaje", "resultado"), ("Juicios pendientes", "pendientes")],
                c["raps"], {"pendientes": FMT_INT}, {"A": 12, "B": 110, "C": 12})
    _hoja_instructores(wb, resultado)

    # Que Excel recalcule las fórmulas al abrir
    wb.calculation.fullCalcOnLoad = True

    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    wb.save(ruta)
    return ruta