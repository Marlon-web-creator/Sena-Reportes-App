"""
modules/estadisticas_juicios.py

Lee el "Reporte consolidado de pendientes" (SVP x Synchronize) y devuelve:
  1. Estadísticas puntuales de los juicios pendientes.
  2. Proyección de reducción para el próximo trimestre.

Notas de diseño
---------------
* El archivo es una FOTO de un día (solo pendientes), no una serie de
  tiempo. Por eso la proyección tiene dos componentes:
    - "estructural": aplica tasas de resolución por tipo de pendiente
      (con programación en Synchronize / solo SVP / sin asignación) en 3
      escenarios. Las tasas son SUPUESTOS editables, no datos del archivo.
    - "histórica": si ya hay >= 2 ejecuciones previas guardadas (distintas
      fechas de reporte), ajusta una tendencia lineal y la extrapola. Se
      vuelve más confiable a medida que se suben reportes semanales.
* Se lee con openpyxl en modo read_only (el archivo tiene ~80 mil filas).
* Las columnas se ubican por el TEXTO del encabezado, no por posición.
"""

import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from openpyxl import load_workbook

MAX_FILAS_BUSQUEDA_ENCABEZADO = 30
VACIOS = {"", "—", "-", "–"}

# clave interna -> encabezado normalizado (sin tildes, minúsculas)
_COLUMNAS = {
    "ficha": "numero de ficha",
    "programa": "programa de formacion",
    "trim_actual": "trimestre actual (ficha)",
    "trim_visto": "trimestre en que vieron el resultado",
    "documento": "documento del aprendiz",
    "nombre": "nombre del aprendiz",
    "estado_aprendiz": "estado del aprendiz",
    "rap": "resultado de aprendizaje",
    "svp": "svp (instructor fila)",
    "sync": "synchronize (instructor fila)",
    "anio_sync": "ano programacion sync",
    "t_sync": "t.ano sync",
    "id_rap": "id rap svp",
    "tipo": "tipo (parcial/completo)",
}
_REQUERIDAS = ("ficha", "programa", "documento", "id_rap", "svp", "sync")

CATEGORIAS = {
    "con_sync": "Visto en Synchronize, falta calificar",
    "solo_svp": "Programado en SVP, sin programación en Synchronize",
    "sin_asignacion": "Sin instructor asignado",
}

# Supuestos por defecto de la proyección (tasa de resolución por categoría
# en un trimestre y nuevos pendientes como % del stock actual).
SUPUESTOS_DEFECTO = {
    "conservador": {"con_sync": 0.50, "solo_svp": 0.25, "sin_asignacion": 0.05, "nuevos_pct": 0.15},
    "base":        {"con_sync": 0.70, "solo_svp": 0.40, "sin_asignacion": 0.10, "nuevos_pct": 0.10},
    "optimista":   {"con_sync": 0.85, "solo_svp": 0.60, "sin_asignacion": 0.25, "nuevos_pct": 0.05},
}


# ---------------------------------------------------------------- utilidades
def _norm(valor) -> str:
    if valor is None:
        return ""
    t = unicodedata.normalize("NFKD", str(valor))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().strip()
    return re.sub(r"\s+", " ", t)


def _txt(valor) -> str:
    return str(valor).strip() if valor is not None else ""


def _lleno(valor) -> bool:
    return _txt(valor) not in VACIOS


def _num(valor):
    try:
        return int(float(valor))
    except (TypeError, ValueError):
        return None


def _doc(valor) -> str:
    t = _txt(valor)
    return t[:-2] if t.endswith(".0") else t


def _pct(parte, total) -> float:
    return round(parte * 100 / total, 1) if total else 0.0


def _parse_fecha(texto) -> date | None:
    if isinstance(texto, datetime):
        return texto.date()
    if isinstance(texto, date):
        return texto
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", _txt(texto))
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def trimestre_calendario(f: date) -> tuple[int, int]:
    return (f.year, (f.month - 1) // 3 + 1)


def siguiente_trimestre(anio: int, t: int) -> tuple[int, int]:
    return (anio + 1, 1) if t == 4 else (anio, t + 1)


def _top(counter: Counter, n: int, clave: str = "nombre") -> list[dict]:
    return [{clave: k, "pendientes": v} for k, v in counter.most_common(n)]


# ------------------------------------------------------------------- lectura
def _leer(archivo: Path, fila_encabezado: int | None):
    """Devuelve (metadatos, filas) donde filas es un iterador de dicts."""
    wb = load_workbook(archivo, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    # hoja con más filas si hubiese varias
    for h in wb.worksheets:
        if (h.max_row or 0) > (ws.max_row or 0):
            ws = h

    meta: dict[str, str] = {}
    cols: dict[str, int] | None = None
    iterador = ws.iter_rows(values_only=True)
    n_fila = 0
    for fila in iterador:
        n_fila += 1
        if fila_encabezado and n_fila != fila_encabezado:
            if n_fila > fila_encabezado:
                break
            continue
        if not fila_encabezado and n_fila > MAX_FILAS_BUSQUEDA_ENCABEZADO:
            break

        normalizados = [_norm(c) for c in fila]
        encontrados = {}
        for clave, enc in _COLUMNAS.items():
            if enc in normalizados:
                encontrados[clave] = normalizados.index(enc)
        if all(c in encontrados for c in _REQUERIDAS):
            cols = encontrados
            break
        # filas previas = metadatos tipo "Etiqueta | valor"
        if len(fila) > 1 and fila[0] is not None and fila[1] is not None:
            meta[_norm(fila[0])] = _txt(fila[1])

    if cols is None:
        wb.close()
        raise ValueError(
            "No se encontró la estructura esperada. Se requieren las columnas: "
            "'Número de ficha', 'Programa de formación', 'Documento del aprendiz', "
            "'ID RAP SVP', 'SVP (instructor fila)' y 'Synchronize (instructor fila)'."
        )

    def filas():
        for fila in iterador:
            if fila is None or all(c is None for c in fila):
                continue
            yield {k: (fila[i] if i < len(fila) else None) for k, i in cols.items()}
        wb.close()

    return meta, filas()


# ----------------------------------------------------------------- principal
def analizar(
    archivo: Path,
    top_n: int = 10,
    supuestos: dict | None = None,
    historico: list[dict] | None = None,
    fila_encabezado: int | None = None,
) -> dict:
    """
    supuestos  : sobreescribe SUPUESTOS_DEFECTO (por escenario y clave).
    historico  : lista [{"fecha": date, "total_pendientes": int}, ...] de
                 ejecuciones previas, para la proyección histórica.
    """
    archivo = Path(archivo)
    meta, filas = _leer(archivo, fila_encabezado)

    total = 0
    por_programa = defaultdict(lambda: {"filas": 0, "aprendices": set(), "fichas": set(), "cat": Counter()})
    por_aprendiz = Counter()
    aprendiz_nombre: dict[str, str] = {}
    por_ficha = Counter()
    por_rap = Counter()
    rap_nombre: dict[int, str] = {}
    svp_cnt, sync_cnt = Counter(), Counter()
    trim_actual, trim_visto, t_sync = Counter(), Counter(), Counter()
    tipo, estado = Counter(), Counter()
    categorias = Counter()
    fichas, aprendices, raps = set(), set(), set()

    for r in filas:
        total += 1
        doc = _doc(r["documento"])
        prog = _txt(r["programa"]) or "SIN PROGRAMA"
        ficha = _txt(r["ficha"])

        if _lleno(r.get("sync")):
            cat = "con_sync"
        elif _lleno(r.get("svp")):
            cat = "solo_svp"
        else:
            cat = "sin_asignacion"
        categorias[cat] += 1

        p = por_programa[prog]
        p["filas"] += 1
        p["aprendices"].add(doc)
        p["fichas"].add(ficha)
        p["cat"][cat] += 1

        por_aprendiz[doc] += 1
        aprendiz_nombre.setdefault(doc, _txt(r.get("nombre")))
        por_ficha[ficha] += 1
        fichas.add(ficha)
        aprendices.add(doc)

        id_rap = _txt(r["id_rap"])
        por_rap[id_rap] += 1
        raps.add(id_rap)
        rap_nombre.setdefault(id_rap, _txt(r.get("rap")))

        if cat != "sin_asignacion":
            if _lleno(r.get("svp")):
                svp_cnt[_txt(r["svp"])] += 1
            if _lleno(r.get("sync")):
                sync_cnt[_txt(r["sync"])] += 1

        trim_actual[_txt(r.get("trim_actual")) or "Sin trimestre"] += 1
        trim_visto[_txt(r.get("trim_visto")) or "No visto"] += 1
        ts = _num(r.get("t_sync"))
        if ts:
            t_sync[f"T{ts}"] += 1
        tipo[_txt(r.get("tipo")) or "Sin dato"] += 1
        estado[_txt(r.get("estado_aprendiz")) or "Sin dato"] += 1

    if total == 0:
        raise ValueError("El archivo no contiene filas de datos debajo del encabezado.")

    fecha_reporte = _parse_fecha(meta.get("fecha de generacion")) or date.today()
    n_aprend = len(aprendices)
    valores = list(por_aprendiz.values())

    # ---------------- estadísticas
    resumen = {
        "fecha_reporte": fecha_reporte.isoformat(),
        "archivo_origen": meta.get("archivo svp", ""),
        "total_pendientes": total,
        "aprendices": n_aprend,
        "fichas": len(fichas),
        "programas": len(por_programa),
        "raps_distintos": len(raps),
        "instructores_svp": len(svp_cnt),
        "instructores_sync": len(sync_cnt),
        "promedio_por_aprendiz": round(total / n_aprend, 1),
        "mediana_por_aprendiz": median(valores),
        "maximo_por_aprendiz": max(valores),
    }

    gestion = [
        {"clave": k, "descripcion": CATEGORIAS[k], "pendientes": categorias[k],
         "pct": _pct(categorias[k], total)}
        for k in CATEGORIAS
    ]

    programas = sorted(
        (
            {
                "programa": nombre,
                "pendientes": d["filas"],
                "pct": _pct(d["filas"], total),
                "aprendices": len(d["aprendices"]),
                "fichas": len(d["fichas"]),
                "promedio_por_aprendiz": round(d["filas"] / len(d["aprendices"]), 1),
                "sin_asignacion": d["cat"]["sin_asignacion"],
            }
            for nombre, d in por_programa.items()
        ),
        key=lambda x: x["pendientes"], reverse=True,
    )

    def _dist(counter: Counter, orden=None):
        claves = orden or sorted(counter, key=lambda k: (k.startswith(("Sin", "No")), k))
        return [{"etiqueta": k, "pendientes": counter[k], "pct": _pct(counter[k], total)}
                for k in claves if k in counter]

    # trimestre más alto observado = fichas próximas a cerrar etapa lectiva
    numeros = [int(k[1:]) for k in trim_actual if re.fullmatch(r"T\d+", k)]
    t_max = f"T{max(numeros)}" if numeros else None

    estadisticas = {
        "resumen": resumen,
        "gestion": gestion,
        "por_programa": programas,
        "por_trimestre_ficha": _dist(trim_actual),
        "por_trimestre_visto": _dist(trim_visto),
        "por_trimestre_programacion_sync": _dist(t_sync),
        "por_tipo": _dist(tipo),
        "por_estado_aprendiz": _dist(estado),
        "riesgo_cierre": {
            "trimestre": t_max,
            "pendientes": trim_actual.get(t_max, 0) if t_max else 0,
            "pct": _pct(trim_actual.get(t_max, 0), total) if t_max else 0.0,
            "nota": "Fichas en el trimestre más alto observado: menos tiempo para resolver.",
        },
        "top_instructores_svp": _top(svp_cnt, top_n),
        "top_instructores_sync": _top(sync_cnt, top_n),
        "top_fichas": [{"ficha": k, "pendientes": v} for k, v in por_ficha.most_common(top_n)],
        "top_aprendices": [
            {"documento": k, "nombre": aprendiz_nombre.get(k, ""), "pendientes": v}
            for k, v in por_aprendiz.most_common(top_n)
        ],
        "top_raps": [
            {"id_rap": k, "resultado": rap_nombre.get(k, "")[:140], "pendientes": v}
            for k, v in por_rap.most_common(top_n)
        ],
    }

    # ---------------- proyección
    anio, t = trimestre_calendario(fecha_reporte)
    anio_sig, t_sig = siguiente_trimestre(anio, t)
    proyeccion = {
        "trimestre_actual": f"T{t} {anio}",
        "trimestre_proyectado": f"T{t_sig} {anio_sig}",
        "estructural": _proyeccion_estructural(
            total, n_aprend, categorias, _mezclar(supuestos)
        ),
        "historica": _proyeccion_historica(historico or [], fecha_reporte, total),
    }

    return {"estadisticas": estadisticas, "proyeccion": proyeccion}


# ---------------------------------------------------------------- proyección
def _mezclar(supuestos: dict | None) -> dict:
    res = {k: dict(v) for k, v in SUPUESTOS_DEFECTO.items()}
    for esc, valores in (supuestos or {}).items():
        if esc in res and isinstance(valores, dict):
            for k, v in valores.items():
                if k in res[esc]:
                    res[esc][k] = min(max(float(v), 0.0), 1.0)
    return res


def _proyeccion_estructural(total, n_aprend, categorias, supuestos) -> dict:
    escenarios = {}
    for nombre, s in supuestos.items():
        resueltos = sum(categorias[c] * s[c] for c in CATEGORIAS)
        nuevos = total * s["nuevos_pct"]
        proyectado = total - resueltos + nuevos

        escenarios[nombre] = {
            "supuestos": s,
            "resueltos": round(resueltos),
            "nuevos": round(nuevos),
            "pendientes_proyectados": round(proyectado),
            "reduccion_neta": round(total - proyectado),
            "reduccion_neta_pct": _pct(total - proyectado, total),
            "promedio_por_aprendiz": round(proyectado / n_aprend, 1) if n_aprend else 0,
        }
    return {
        "base_pendientes": total,
        "escenarios": escenarios,
        "advertencia": (
            "Las tasas de resolución y de pendientes nuevos son supuestos editables, "
            "no datos del archivo. Ajústalos con el comportamiento real de tu centro."
        ),
    }


def _proyeccion_historica(historico: list[dict], fecha_actual: date, total_actual: int) -> dict:
    # una observación por fecha (la última gana) + la ejecución actual
    puntos = {h["fecha"]: h["total_pendientes"] for h in historico if h.get("fecha")}
    puntos[fecha_actual] = total_actual
    serie = sorted(puntos.items())

    if len(serie) < 2:
        return {
            "disponible": False,
            "puntos": len(serie),
            "mensaje": "Se necesitan al menos 2 reportes de fechas distintas para calcular tendencia. "
                       "Sube reportes periódicos (p. ej. semanales) y esta proyección se activará sola.",
        }

    x0 = serie[0][0]
    xs = [(f - x0).days for f, _ in serie]
    ys = [v for _, v in serie]
    n = len(serie)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    pendiente = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    intercepto = my - pendiente * mx

    destino = fecha_actual + timedelta(days=91)
    x_dest = (destino - x0).days
    proyectado = max(0, round(intercepto + pendiente * x_dest))

    return {
        "disponible": True,
        "puntos": n,
        "serie": [{"fecha": f.isoformat(), "total_pendientes": v} for f, v in serie],
        "cambio_por_dia": round(pendiente, 1),
        "fecha_proyeccion": destino.isoformat(),
        "pendientes_proyectados": proyectado,
        "reduccion_neta": total_actual - proyectado,
        "reduccion_neta_pct": _pct(total_actual - proyectado, total_actual),
        "confiabilidad": "baja" if n < 4 else "media" if n < 8 else "alta",
    }