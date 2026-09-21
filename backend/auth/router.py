"""
auth/router.py

Endpoints de sesión:
- POST /api/auth/login    → valida contraseña, emite cookie.
- POST /api/auth/logout   → borra cookie.
- GET  /api/auth/me       → confirma si hay sesión (usado por el frontend).
"""

from collections import defaultdict
from time import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from auth.security import (
    verificar_password,
    crear_sesion,
    cerrar_sesion,
    requiere_auth,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Rate limit simple en memoria: 5 intentos por IP cada 5 minutos.
_intentos: dict[str, list[float]] = defaultdict(list)
MAX_INTENTOS = 5
VENTANA = 300


def _rate_limit(ip: str) -> None:
    ahora = time()
    _intentos[ip] = [t for t in _intentos[ip] if ahora - t < VENTANA]
    if len(_intentos[ip]) >= MAX_INTENTOS:
        raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
    _intentos[ip].append(ahora)


class Credenciales(BaseModel):
    password: str


@router.post("/login")
def login(datos: Credenciales, request: Request, response: Response):
    # En Render estamos detrás de proxy: la IP real viene en X-Forwarded-For.
    ip = (
        request.headers.get("x-forwarded-for", request.client.host or "?")
        .split(",")[0]
        .strip()
    )
    _rate_limit(ip)

    if not verificar_password(datos.password):
        raise HTTPException(401, "Contraseña incorrecta")

    crear_sesion(response)
    _intentos.pop(ip, None)  # reset al acertar
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    cerrar_sesion(response)
    return {"ok": True}


@router.get("/me", dependencies=[Depends(requiere_auth)])
def me():
    return {"autenticado": True}