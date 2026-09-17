# SENA Reportes App

## Función del backend

El backend es una API desarrollada con FastAPI que conecta la interfaz web con los procesos de generación y procesamiento de reportes del proyecto. Sus funciones principales son:

- Recibir y validar archivos Excel y PDF cargados desde el frontend.
- Ejecutar los módulos de consolidación de no aprobados, depuración, correos de aprendices y verificación de no programados.
- Guardar los archivos cargados y los resultados generados en `backend/storage`.
- Registrar en la base de datos el historial, los parámetros y los resultados de cada ejecución.
- Informar el progreso de los procesos que se ejecutan en segundo plano y permitir descargar sus resultados.
- Servir los archivos estáticos del frontend y exponer los servicios de la aplicación mediante rutas `/api`.

## Cómo ejecutar en local

En bash:
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000


Luego abrir http://127.0.0.1:8000 en el navegador.

## Estado actual proyecto general

- Arreglar el Scrapper, llega hasta "Buscar Ficha de Caracterizacion" y se detiene, revisar que puede estar fallando para adaptar el codigo de acuerdo al viejo

14/09/2026