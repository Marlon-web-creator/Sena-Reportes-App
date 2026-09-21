"""
auth/security.py

Contraseña única + cookie firmada (itsdangerous) + bcrypt.
Se usa únicamente para proteger la página Base de Datos y sus endpoints
de administración (listar, crear, mover, borrar).

Variables de entorno requeridas:
- SESSION_SECRET     → cadena aleatoria larga.
- APP_PASSWORD_HASH  → hash bcrypt de la contraseña de acceso.
"""

import os
import bcrypt
from fastapi import HTTPException, Request, Response, status
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

SESSION_SECRET = os.environ.get("SESSION_SECRET")
APP_PASSWORD_HASH = os.environ.get("APP_PASSWORD_HASH")

if not SESSION_SECRET or not APP_PASSWORD_HASH:
    raise RuntimeError(
        "Faltan SESSION_SECRET y/o APP_PASSWORD_HASH en las variables de entorno."
    )

COOKIE_NAME = "sena_db_session"
MAX_AGE = 60 * 60 * 8  # 8 horas

_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="sena-db")


def verificar_password(password: str) -> bool:
    """Compara la contraseña recibida contra el hash bcrypt del entorno."""
    try:
        return bcrypt.checkpw(password.encode(), APP_PASSWORD_HASH.encode())
    except Exception:
        return False


def crear_sesion(response: Response) -> None:
    """Emite la cookie de sesión firmada."""
    token = _serializer.dumps({"ok": True})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=MAX_AGE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


def cerrar_sesion(response: Response) -> None:
    """Borra la cookie de sesión."""
    response.delete_cookie(COOKIE_NAME, path="/")


def requiere_auth(request: Request) -> None:
    """Dependencia FastAPI: lanza 401 si no hay cookie válida."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No autenticado")
    try:
        _serializer.loads(token, max_age=MAX_AGE)
    except SignatureExpired:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión expirada")
    except BadSignature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión inválida")