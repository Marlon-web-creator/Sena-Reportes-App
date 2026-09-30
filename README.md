# SENA Reportes App

Aplicación web desarrollada para facilitar el procesamiento, consolidación y generación de reportes a partir de archivos utilizados en los procesos del SENA.

El proyecto está compuesto por un **backend desarrollado con Python y FastAPI** y un **frontend desarrollado con HTML, CSS y JavaScript**.

La aplicación permite cargar archivos, procesarlos mediante diferentes funcionalidades, almacenar los resultados y consultar el estado de los procesos que requieren mayor tiempo de ejecución.

---

## Tecnologías utilizadas

### Backend

* Python
* FastAPI
* Uvicorn
* SQLite
* Pandas
* OpenPyXL
* xlrd
* PyMuPDF
* Pytesseract
* Pillow
* Playwright
* Cryptography
* Python Multipart

### Frontend

* HTML5
* CSS3
* JavaScript
* Fetch API
* Toastify.js

### Base de datos

* SQLite

---

# Estructura del proyecto

La estructura principal del proyecto es:

```text
Sena-Reportes-App/
│
├── backend/
│   ├── main.py
│   ├── database.py
│   ├── progreso.py
│   ├── requirements.txt
│   │
│   ├── modules/
│   │   ├── __init__.py
│   │   ├── correo_aprendices.py
│   │   ├── depuracion.py
│   │   ├── file_utils.py
│   │   ├── no_aprobados.py
│   │   └── no_programados.py
│   │
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── archivos_router.py
│   │   ├── consolidador_router.py
│   │   ├── correo_router.py
│   │   ├── depuracion_router.py
│   │   └── no_programados_router.py
│   │
│   └── storage/
│       ├── uploads/
│       └── outputs/
│
├── frontend/
│   ├── index.html
│   ├── no-aprobados.html
│   ├── depuracion.html
│   ├── no-programados.html
│   ├── correos.html
│   ├── base-datos.html
│   │
│   ├── css/
│   │   └── styles.css
│   │
│   └── js/
│       ├── api.js
│       ├── base-datos.js
│       ├── correos.js
│       ├── depuracion.js
│       ├── modal.js
│       ├── no-aprobados.js
│       ├── no-programados.js
│       └── toasts.js
│
└── app.db
```

---

# Arquitectura general

La aplicación funciona mediante la comunicación entre el frontend y el backend.

El flujo general es:

```text
Usuario
   │
   ▼
Frontend
HTML + CSS + JavaScript
   │
   │ Solicitudes HTTP
   ▼
FastAPI
   │
   ├── Routers
   │
   ├── Modules
   │
   ├── Database
   │
   └── Storage
   │
   ▼
Resultado
   │
   ▼
Frontend
```

El frontend se encarga de la interfaz y de enviar las solicitudes al backend.

El backend recibe las solicitudes, procesa los archivos, ejecuta las funciones correspondientes y devuelve los resultados.

---

# Backend

El backend se encuentra en:

```text
backend/
```

Su función principal es proporcionar la API que utiliza el frontend para ejecutar los diferentes procesos de la aplicación.

También se encarga de:

* Recibir archivos.
* Validar información.
* Ejecutar los procesos de cada funcionalidad.
* Guardar archivos.
* Generar archivos de salida.
* Registrar ejecuciones en la base de datos.
* Consultar el progreso de procesos.
* Servir el frontend.

---

# `main.py`

El archivo:

```text
backend/main.py
```

es el punto de entrada principal de la aplicación.

Aquí se crea y configura la aplicación FastAPI.

También se registran los diferentes routers utilizados por el proyecto.

Además, el backend sirve directamente los archivos del frontend mediante `StaticFiles`.

La configuración utiliza la carpeta:

```text
frontend/
```

como raíz de los archivos estáticos.

Esto permite acceder directamente a las páginas desde el mismo servidor.

Por ejemplo:

```text
http://127.0.0.1:8000/
```

carga:

```text
frontend/index.html
```

Mientras que:

```text
http://127.0.0.1:8000/no-programados.html
```

carga:

```text
frontend/no-programados.html
```

Por esta razón, el proyecto no necesita ejecutar un servidor frontend separado para funcionar.

---

# Routers

Los routers se encuentran en:

```text
backend/routers/
```

Su función es manejar las solicitudes HTTP realizadas por el frontend.

Los routers reciben los archivos y parámetros enviados por el usuario, ejecutan las funciones correspondientes y devuelven las respuestas.

Actualmente existen los siguientes routers:

```text
backend/routers/
├── archivos_router.py
├── consolidador_router.py
├── correo_router.py
├── depuracion_router.py
└── no_programados_router.py
```

---

## `archivos_router.py`

Contiene las rutas relacionadas con la gestión de archivos utilizados por la aplicación.

Se encarga de las operaciones relacionadas con los archivos que llegan al backend y su almacenamiento.

---

## `consolidador_router.py`

Contiene las rutas relacionadas con los procesos de consolidación.

Está relacionado principalmente con el procesamiento de información utilizado por la funcionalidad de **No Aprobados**.

---

## `correo_router.py`

Contiene las rutas relacionadas con la funcionalidad de **Correos de Aprendices**.

Permite al frontend enviar la información necesaria para ejecutar este proceso y obtener los resultados correspondientes.

---

## `depuracion_router.py`

Contiene las rutas relacionadas con el proceso de **Depuración**.

---

## `no_programados_router.py`

Contiene las rutas relacionadas con el proceso de **No Programados**.

Debido a que este proceso puede requerir un tiempo considerable para terminar, también maneja la creación y consulta del progreso de las ejecuciones.

---

# Modules

La lógica de procesamiento se encuentra en:

```text
backend/modules/
```

Actualmente existen los siguientes módulos:

```text
backend/modules/
├── correo_aprendices.py
├── depuracion.py
├── file_utils.py
├── no_aprobados.py
└── no_programados.py
```

Los módulos contienen la lógica que realiza realmente el procesamiento de los datos.

Los routers se encargan principalmente de recibir las solicitudes y los módulos se encargan de ejecutar los procesos.

---

## `no_aprobados.py`

Contiene la lógica utilizada para procesar y consolidar la información relacionada con los aprendices no aprobados.

Este proceso trabaja con la información recibida mediante los archivos cargados desde el frontend.

---

## `depuracion.py`

Contiene la lógica utilizada para el proceso de depuración de información.

Se utiliza para procesar los datos correspondientes a esta funcionalidad y generar los resultados necesarios.

---

## `no_programados.py`

Contiene la lógica utilizada para procesar la información correspondiente a los aprendices o competencias no programadas.

Este módulo puede trabajar con diferentes archivos de entrada y realizar el procesamiento necesario para generar el resultado correspondiente.

También contiene procesos relacionados con el manejo de información obtenida desde archivos PDF.

---

## `correo_aprendices.py`

Contiene la lógica utilizada para obtener y procesar información relacionada con los correos electrónicos de los aprendices.

Este proceso trabaja con información proveniente de los archivos utilizados por la aplicación.

---

## `file_utils.py`

Contiene funciones auxiliares relacionadas con el manejo de archivos.

Estas funciones pueden ser utilizadas por diferentes módulos para evitar repetir código relacionado con operaciones comunes de archivos.

---

# Progreso de procesos

El archivo:

```text
backend/progreso.py
```

contiene la lógica relacionada con el seguimiento del progreso de determinados procesos.

Esto permite que el frontend pueda conocer el estado de una ejecución sin tener que esperar a que termine toda la operación dentro de una única solicitud.

Los estados utilizados son:

```text
procesando
listo
error
```

El funcionamiento general es:

```text
Frontend
   │
   │ Inicia proceso
   ▼
Backend
   │
   │ Crea ejecución
   ▼
Proceso en segundo plano
   │
   ├── procesando
   │
   ├── listo
   │
   └── error
   │
   ▼
Frontend consulta el progreso
```

Esto es utilizado especialmente por los procesos que pueden tardar más tiempo en ejecutarse.

---

# Base de datos

La aplicación utiliza **SQLite** como sistema de base de datos.

El archivo principal es:

```text
app.db
```

La lógica de conexión y operaciones de la base de datos se encuentra en:

```text
backend/database.py
```

La base de datos se utiliza para almacenar información relacionada con las ejecuciones realizadas y los archivos cargados.

---

## Tabla `ejecuciones`

La tabla `ejecuciones` almacena información relacionada con los procesos ejecutados.

Entre la información registrada se encuentra:

* Identificador de la ejecución.
* Módulo utilizado.
* Fecha de ejecución.
* Parámetros utilizados.
* Resultado.
* Archivos generados.

Algunos datos se almacenan en formato JSON para permitir guardar información estructurada de cada ejecución.

---

## Tabla `archivos_subidos`

La tabla `archivos_subidos` almacena información sobre los archivos cargados en la aplicación.

Entre la información registrada se encuentra:

* Nombre original del archivo.
* Nombre utilizado internamente.
* Ruta.
* Tamaño en bytes.
* Módulo relacionado.
* Fecha de carga.

Las fechas utilizadas por la aplicación manejan la zona horaria:

```text
America/Bogota
```

---

# Almacenamiento de archivos

Los archivos utilizados por los procesos se almacenan dentro de:

```text
backend/storage/
```

La estructura principal es:

```text
backend/storage/
├── uploads/
└── outputs/
```

---

## `uploads`

La carpeta:

```text
backend/storage/uploads/
```

contiene los archivos cargados por el usuario.

Estos archivos son utilizados posteriormente por los diferentes procesos del backend.

---

## `outputs`

La carpeta:

```text
backend/storage/outputs/
```

contiene los archivos generados como resultado de los procesos.

Por ejemplo, los procesos pueden generar archivos Excel que posteriormente pueden ser descargados desde el frontend.

---

# Frontend

El frontend se encuentra en:

```text
frontend/
```

Está desarrollado utilizando:

* HTML
* CSS
* JavaScript

No utiliza actualmente frameworks como React, Vue o Angular.

El frontend se comunica con el backend mediante solicitudes HTTP utilizando principalmente la API `fetch` de JavaScript.

---

# Páginas del frontend

Actualmente existen las siguientes páginas principales:

```text
frontend/
├── index.html
├── no-aprobados.html
├── depuracion.html
├── no-programados.html
├── correos.html
└── base-datos.html
```

---

## `index.html`

Es la página principal de la aplicación.

Desde ella se puede acceder a las diferentes funcionalidades disponibles.

---

## `no-aprobados.html`

Interfaz correspondiente al proceso de **No Aprobados**.

Permite al usuario interactuar con el backend para cargar la información necesaria y ejecutar el proceso correspondiente.

Su lógica JavaScript se encuentra en:

```text
frontend/js/no-aprobados.js
```

---

## `depuracion.html`

Interfaz correspondiente al proceso de **Depuración**.

Su lógica JavaScript se encuentra en:

```text
frontend/js/depuracion.js
```

---

## `no-programados.html`

Interfaz correspondiente al proceso de **No Programados**.

Permite cargar los archivos necesarios y ejecutar el proceso.

Debido a que este proceso puede tardar, la página también puede consultar el progreso de la ejecución.

Su lógica JavaScript se encuentra en:

```text
frontend/js/no-programados.js
```

---

## `correos.html`

Interfaz correspondiente al proceso de **Correos de Aprendices**.

Su lógica JavaScript se encuentra en:

```text
frontend/js/correos.js
```

---

## `base-datos.html`

Interfaz relacionada con la consulta de información almacenada en la base de datos de la aplicación.

Su lógica JavaScript se encuentra en:

```text
frontend/js/base-datos.js
```

---

# JavaScript

Los archivos JavaScript se encuentran en:

```text
frontend/js/
```

Actualmente existen:

```text
frontend/js/
├── api.js
├── base-datos.js
├── correos.js
├── depuracion.js
├── modal.js
├── no-aprobados.js
├── no-programados.js
└── toasts.js
```

---

## `api.js`

Contiene funciones reutilizables para comunicarse con el backend.

Entre las operaciones utilizadas se encuentran las solicitudes `GET` y `POST`.

Por ejemplo:

```javascript
apiGet(...)
apiPost(...)
```

La finalidad es evitar repetir la misma lógica de comunicación con la API en cada página.

---

## `base-datos.js`

Contiene la lógica de la página de consulta de la base de datos.

---

## `correos.js`

Contiene la lógica de interacción de la página de correos de aprendices.

---

## `depuracion.js`

Contiene la lógica de interacción de la página de depuración.

---

## `no-aprobados.js`

Contiene la lógica de interacción de la página de no aprobados.

---

## `no-programados.js`

Contiene la lógica de interacción de la página de no programados.

También maneja la comunicación necesaria para consultar el progreso de los procesos que se ejecutan en segundo plano.

---

## `modal.js`

Contiene funciones relacionadas con los componentes de tipo modal utilizados en la interfaz.

---

## `toasts.js`

Contiene funciones relacionadas con los mensajes emergentes utilizados para informar al usuario sobre diferentes estados de la aplicación.

---

# CSS

Los estilos principales de la aplicación se encuentran en:

```text
frontend/css/styles.css
```

Este archivo contiene los estilos generales utilizados por las diferentes páginas del proyecto.

Los cambios relacionados con:

* Colores.
* Espaciados.
* Botones.
* Formularios.
* Elementos visuales.
* Diseño general.

se realizan principalmente desde este archivo.

---

# Comunicación Frontend - Backend

La comunicación entre frontend y backend se realiza mediante solicitudes HTTP.

El flujo general es:

```text
Página HTML
     │
     ▼
JavaScript
     │
     ▼
api.js
     │
     ▼
FastAPI
     │
     ▼
Router
     │
     ▼
Module
     │
     ├── Database
     │
     └── Storage
     │
     ▼
Respuesta
     │
     ▼
JavaScript
     │
     ▼
Interfaz
```

Por ejemplo, cuando un usuario carga un archivo:

```text
1. Usuario selecciona archivo
        ↓
2. JavaScript obtiene el archivo
        ↓
3. Se realiza una petición al backend
        ↓
4. FastAPI recibe el archivo
        ↓
5. El router procesa la solicitud
        ↓
6. El módulo ejecuta el proceso
        ↓
7. Se almacena el resultado
        ↓
8. El backend devuelve la respuesta
        ↓
9. El frontend muestra el resultado
```

---

# API

Las rutas utilizadas por el frontend se encuentran bajo el prefijo:

```text
/api
```

Los routers son los encargados de definir los endpoints correspondientes a cada funcionalidad.

Un ejemplo de las rutas utilizadas para procesos con seguimiento de progreso es:

```text
POST /api/no-programados
```

para iniciar el proceso y:

```text
GET /api/no-programados/progreso/{id}
```

para consultar el progreso de una ejecución.

El identificador de ejecución permite relacionar la consulta con el proceso que fue iniciado previamente.

---

# Proceso de No Programados

El proceso de No Programados utiliza un identificador de ejecución para organizar los archivos y controlar el progreso.

De forma general:

```text
Frontend
   │
   │ Archivos
   ▼
POST /api/no-programados
   │
   ▼
Backend
   │
   ├── Crea ID de ejecución
   │
   ├── Guarda archivos
   │
   └── Inicia procesamiento
   │
   ▼
Proceso
   │
   ├── Procesando
   │
   ├── Listo
   │
   └── Error
   │
   ▼
Frontend
   │
   └── Consulta progreso
```

Los archivos relacionados con una ejecución pueden organizarse utilizando su identificador.

Por ejemplo:

```text
backend/storage/uploads/<id_ejecucion>/
```

y:

```text
backend/storage/outputs/<id_ejecucion>/
```

Esto permite mantener separados los archivos correspondientes a diferentes ejecuciones.

---

# Instalación

## Requisitos

Para ejecutar el proyecto se necesita:

* Python 3
* pip
* Git

---

## Crear entorno virtual

Desde la raíz del proyecto:

```bash
python -m venv .venv
```

En Windows:

```bash
.venv\Scripts\activate
```

En Linux o macOS:

```bash
source .venv/bin/activate
```

---

## Instalar dependencias

Entrar a la carpeta del backend:

```bash
cd backend
```

Instalar las dependencias:

```bash
pip install -r requirements.txt
```

Si el proyecto requiere Playwright:

```bash
playwright install
```

---

# Ejecutar la aplicación

Desde la carpeta:

```text
backend/
```

ejecutar:

```bash
python -m uvicorn main:app --reload --port 8000
```

La aplicación estará disponible en:

```text
http://127.0.0.1:8000
```

También se puede acceder mediante:

```text
http://localhost:8000
```

El parámetro:

```text
--reload
```

permite que Uvicorn reinicie automáticamente el servidor cuando detecta cambios en los archivos durante el desarrollo.

---

# Flujo de trabajo para modificar el proyecto

Para modificar una funcionalidad existente, primero se debe identificar qué parte del proyecto corresponde al cambio.

| Si se quiere modificar...       | Archivo o carpeta          |
| ------------------------------- | -------------------------- |
| Una página                      | `frontend/*.html`          |
| El diseño                       | `frontend/css/styles.css`  |
| El comportamiento de una página | `frontend/js/`             |
| La comunicación con la API      | `frontend/js/api.js`       |
| Una ruta de la API              | `backend/routers/`         |
| La lógica de procesamiento      | `backend/modules/`         |
| La base de datos                | `backend/database.py`      |
| El progreso de procesos         | `backend/progreso.py`      |
| El almacenamiento de archivos   | `backend/storage/`         |
| La configuración principal      | `backend/main.py`          |
| Las dependencias de Python      | `backend/requirements.txt` |

---

# Separación de responsabilidades

El proyecto mantiene una separación entre las diferentes partes de la aplicación.

## Frontend

Se encarga de:

* Mostrar la interfaz.
* Recibir información del usuario.
* Seleccionar archivos.
* Enviar solicitudes al backend.
* Mostrar mensajes.
* Mostrar resultados.
* Consultar el progreso de los procesos.

## Routers

Se encargan de:

* Recibir solicitudes HTTP.
* Recibir archivos.
* Recibir parámetros.
* Llamar a los módulos correspondientes.
* Devolver respuestas al frontend.

## Modules

Se encargan de:

* Procesar la información.
* Leer archivos.
* Transformar datos.
* Generar resultados.
* Ejecutar la lógica específica de cada funcionalidad.

## Database

Se encarga de:

* Guardar información de las ejecuciones.
* Guardar información de los archivos cargados.
* Consultar información almacenada.

## Storage

Se encarga de almacenar físicamente:

* Archivos cargados.
* Archivos generados.
* Archivos relacionados con las ejecuciones.

---

# Recomendaciones para modificar código existente

Antes de realizar cambios en una funcionalidad, es importante revisar el flujo completo de la misma.

Por ejemplo:

```text
HTML
 ↓
JavaScript
 ↓
API
 ↓
Router
 ↓
Module
 ↓
Database / Storage
 ↓
Resultado
```

Esto permite identificar correctamente dónde debe realizarse el cambio y evita colocar lógica en una ubicación incorrecta.

La lógica de procesamiento debe permanecer principalmente en los módulos, mientras que los routers deben encargarse de la comunicación entre el frontend y el backend.

---

# Archivos generados

Los procesos de la aplicación pueden generar archivos dentro de:

```text
backend/storage/outputs/
```

Estos archivos corresponden a los resultados de los diferentes procesos.

Los archivos de entrada cargados por el usuario se almacenan dentro de:

```text
backend/storage/uploads/
```

---

# Base de datos y archivos

La aplicación utiliza dos mecanismos diferentes para almacenar información:

```text
SQLite
  │
  └── Información sobre ejecuciones y archivos

Storage
  │
  ├── Archivos cargados
  └── Archivos generados
```

La base de datos no reemplaza el almacenamiento de archivos.

En la base de datos se guarda información sobre los archivos, mientras que los archivos físicos permanecen dentro de `backend/storage`.

---