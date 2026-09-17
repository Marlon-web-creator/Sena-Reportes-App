"""
progreso.py

Registro en memoria del progreso de tareas en segundo plano (hilos).
Pensado para uso local de un solo usuario.
"""

import threading

_lock = threading.Lock()
_estado: dict[str, dict] = {}


def iniciar(id_ejecucion: str):
    with _lock:
        _estado[id_ejecucion] = {
            "estado": "procesando", "mensaje": "Iniciando…",
            "actual": 0, "total": 0, "resultado": None, "error": None,
            "cancelar": False,
        }


def actualizar(id_ejecucion: str, mensaje: str, actual: int, total: int):
    with _lock:
        if id_ejecucion in _estado:
            _estado[id_ejecucion].update(mensaje=mensaje, actual=actual, total=total)


def finalizar_ok(id_ejecucion: str, resultado: dict):
    with _lock:
        if id_ejecucion in _estado:
            _estado[id_ejecucion].update(estado="listo", resultado=resultado)


def finalizar_error(id_ejecucion: str, error: str):
    with _lock:
        if id_ejecucion in _estado:
            _estado[id_ejecucion].update(estado="error", error=error)


def consultar(id_ejecucion: str):
    with _lock:
        return dict(_estado[id_ejecucion]) if id_ejecucion in _estado else None


def cancelar(id_ejecucion: str):
    """Marca la tarea para que se detenga en el próximo punto de control.
    No la detiene de inmediato: la tarea en segundo plano debe revisar
    `fue_cancelado()` entre pasos (p. ej. entre ficha y ficha) y cortar ahí."""
    with _lock:
        if id_ejecucion in _estado:
            _estado[id_ejecucion]["cancelar"] = True


def fue_cancelado(id_ejecucion: str) -> bool:
    with _lock:
        return _estado.get(id_ejecucion, {}).get("cancelar", False)


def finalizar_cancelado(id_ejecucion: str):
    with _lock:
        if id_ejecucion in _estado:
            _estado[id_ejecucion].update(estado="cancelado")