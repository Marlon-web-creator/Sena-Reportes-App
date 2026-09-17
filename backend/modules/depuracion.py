"""
modules/depuracion.py

Lógica de Depuracion.py adaptada: en vez de pedir archivo y límite por
consola, recibe una ruta de archivo y un límite, y devuelve estadísticas
en lugar de imprimirlas.
"""

from collections import defaultdict
from pathlib import Path

from openpyxl.styles import PatternFill

from .file_utils import cargar_workbook_compatible, es_excel

RELLENO_ROJO = PatternFill(fill_type="solid", fgColor="FF0000")
COLOR_PESTANA_ROJO = "FF0000"


def depurar(archivo: Path, limite: int, salida_dir: Path) -> dict:
    """
    Marca en rojo (celda y pestaña) a las personas que superan `limite`
    juicios "POR EVALUAR" en cada hoja del archivo. Guarda el resultado
    en salida_dir y devuelve estadísticas + ruta del archivo generado.
    """
    archivo = Path(archivo)

    if not es_excel(archivo.name):
        raise ValueError(f"Formato no soportado: {archivo.suffix}")

    salida_dir.mkdir(parents=True, exist_ok=True)

    wb = cargar_workbook_compatible(archivo)

    total_personas_marcadas = 0
    total_hojas_marcadas = 0
    detalle_hojas = []

    for ws in wb.worksheets:
        personas_por_evaluar = defaultdict(int)

        # Primer recorrido: contar "POR EVALUAR" por persona
        for fila in range(2, ws.max_row + 1):
            nombre = ws.cell(fila, 5).value
            apellido = ws.cell(fila, 6).value
            juicio = ws.cell(fila, 9).value

            nombre_limpio = str(nombre).strip() if nombre is not None else ""
            apellido_limpio = str(apellido).strip() if apellido is not None else ""
            juicio_limpio = str(juicio).strip().upper() if juicio is not None else ""

            if nombre_limpio and apellido_limpio:
                persona = (nombre_limpio.upper(), apellido_limpio.upper())
                if juicio_limpio == "POR EVALUAR":
                    personas_por_evaluar[persona] += 1

        personas_mas_de_limite = {
            persona for persona, cantidad in personas_por_evaluar.items() if cantidad > limite
        }

        hoja_marcada = False
        if personas_mas_de_limite:
            ws.sheet_properties.tabColor = COLOR_PESTANA_ROJO
            hoja_marcada = True
            total_hojas_marcadas += 1

        # Segundo recorrido: pintar celdas
        personas_marcadas_en_hoja = set()
        for fila in range(2, ws.max_row + 1):
            nombre = ws.cell(fila, 5).value
            apellido = ws.cell(fila, 6).value

            nombre_limpio = str(nombre).strip() if nombre is not None else ""
            apellido_limpio = str(apellido).strip() if apellido is not None else ""
            persona = (nombre_limpio.upper(), apellido_limpio.upper())

            if persona in personas_mas_de_limite:
                ws.cell(fila, 5).fill = RELLENO_ROJO
                ws.cell(fila, 6).fill = RELLENO_ROJO
                personas_marcadas_en_hoja.add(persona)

        total_personas_marcadas += len(personas_marcadas_en_hoja)

        detalle_hojas.append({
            "hoja": ws.title,
            "marcada": hoja_marcada,
            "personas_marcadas": len(personas_marcadas_en_hoja),
        })

    nombre_salida = Path(archivo).stem + "_Procesado.xlsx"
    ruta_salida = salida_dir / nombre_salida
    wb.save(ruta_salida)

    return {
        "limite": limite,
        "total_hojas_marcadas": total_hojas_marcadas,
        "total_personas_marcadas": total_personas_marcadas,
        "detalle_hojas": detalle_hojas,
        "archivo_generado": str(ruta_salida),
    }