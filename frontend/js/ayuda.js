// ayuda.js — botón "?" de ayuda para cada módulo.
//
// Uso: incluir UNA línea al final de cada página de módulo:
//   <script src="js/ayuda.js"></script>
//
// Detecta solo en qué página está (por el nombre del .html), inyecta el botón
// dentro del <h1> del encabezado y abre un modal con las instrucciones.
// No toca ni depende de la lógica de los módulos (js/no-aprobados.js, etc.).
//
// Para editar un texto o agregar un módulo nuevo: solo cambia AYUDA_MODULOS.

const AYUDA_MODULOS = {
  "no-aprobados.html": {
    titulo: "Consolidador No Aprobados / Por Evaluar",
    resumen:
      "Toma los reportes de Juicios de Evaluación descargados de Sofia Plus y genera un Excel consolidado con los aprendices que tienen juicios NO APROBADO o POR EVALUAR.",
    pasos: [
      "Elige en «Filtro a consolidar» si quieres NO APROBADO o POR EVALUAR.",
      "Marca «Generar también un consolidado general» si quieres además un solo libro con todas las fichas.",
      "Selecciona uno o varios archivos «Reporte Juicios de Evaluación» (.xls o .xlsx), uno por ficha.",
      "Pulsa «Procesar» y espera a que termine.",
    ],
    resultado:
      "En «2. Resultado» verás las estadísticas y los enlaces para descargar los archivos generados. Cada proceso queda guardado en «3. Historial reciente».",
    notas: [
      "Puedes seleccionar varios archivos a la vez; la lista aparece debajo del campo antes de procesar.",
    ],
  },

  "depuracion.html": {
    titulo: "Depuración de «Por Evaluar»",
    resumen:
      "Revisa un Consolidado y marca en rojo a las personas que superan un número máximo de juicios «POR EVALUAR».",
    pasos: [
      "Escribe en «Límite de POR EVALUAR a superar» el número máximo permitido (por defecto 20).",
      "Selecciona el archivo «Consolidado» en formato .xlsx.",
      "Pulsa «Procesar».",
    ],
    resultado:
      "En «2. Resultado» verás las estadísticas y el enlace para descargar el Excel con las personas que superan el límite resaltadas en rojo. Queda registrado en el historial.",
    notas: ["Solo se aceptan archivos .xlsx."],
  },

  "no-programados.html": {
    titulo: "Verificador No Programados",
    resumen:
      "Compara un Consolidado contra los archivos de la Base de Datos para verificar qué competencias están programadas y cuáles no.",
    pasos: [
      "Verifica primero que los archivos necesarios estén cargados en la sección «Base de Datos» con el módulo «No Programados» (organizados en subcarpetas por ficha, si aplica).",
      "Selecciona el «Consolidado» a revisar (.xlsx o .xlsm).",
      "Pulsa «Procesar» y sigue la barra de progreso hasta que termine.",
    ],
    resultado:
      "En «2. Resultado» verás las estadísticas y el archivo generado para descargar. Queda registrado en el historial.",
    notas: [
      "El sistema detecta automáticamente los archivos de la Base de Datos; no hay que adjuntarlos aquí.",
      "Si falta alguno, súbelo primero desde la sección «Base de Datos». Si no tienes acceso, contacta al administrador del sistema.",
    ],
  },

  "correos.html": {
    titulo: "Correo de Aprendices",
    resumen:
      "Extrae los correos de los Reportes de Aprendices y los agrega al Consolidado que elijas.",
    pasos: [
      "Sube el «Consolidado» al que quieres agregar los correos (.xls, .xlsx o .xlsm).",
      "Carga uno o varios «Reportes de Aprendices» de los que se extraerán los correos.",
      "Revisa la lista de archivos seleccionados y pulsa «Procesar».",
    ],
    resultado:
      "En «2. Resultado» verás las estadísticas y el enlace para descargar el Excel generado. Queda registrado en el historial.",
    notas: [],
  },

  "juicios-practica.html": {
    titulo: "Juicios posteriores a Etapa Práctica",
    resumen:
      "Verifica si hay aprendices con juicios de evaluación registrados después de haber aprobado la Etapa Práctica.",
    pasos: [
      "Sube uno o varios «Reporte de Juicios de Evaluación» (.xls, .xlsx o .xlsm), uno por ficha. Las columnas se detectan automáticamente.",
      "Opcional: marca «Comparar solo el día (ignorar la hora)» si no quieres que la hora influya en la comparación de fechas.",
      "Pulsa «Verificar juicios».",
    ],
    resultado:
      "Aparece la sección «Resultado» con el botón para descargar el Excel, un resumen general, un resumen por ficha y la lista de aprendices a revisar. También se muestran las fichas sin práctica aprobada y las advertencias, si las hay.",
    notas: [
      "Al final del resultado, «Columnas detectadas por archivo» sirve para comprobar que el sistema leyó bien cada reporte.",
    ],
  },
};

(function () {
  const pagina = location.pathname.split("/").pop() || "index.html";
  const ayuda = AYUDA_MODULOS[pagina];
  if (!ayuda) return; // página sin ayuda registrada: no hace nada

  function crearLista(tag, items) {
    const lista = document.createElement(tag);
    items.forEach((texto) => {
      const li = document.createElement("li");
      li.textContent = texto; // textContent: sin riesgo de inyección HTML
      lista.appendChild(li);
    });
    return lista;
  }

  function seccion(overlayBody, titulo, nodo) {
    const h4 = document.createElement("h4");
    h4.textContent = titulo;
    overlayBody.appendChild(h4);
    overlayBody.appendChild(nodo);
  }

  function abrirAyuda(botonOrigen) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal modal-ayuda" role="dialog" aria-modal="true" aria-labelledby="ayuda-titulo">
        <h3 id="ayuda-titulo"></h3>
        <div class="ayuda-cuerpo"></div>
        <div class="modal-actions">
          <button class="modal-btn-confirm" type="button">Entendido</button>
        </div>
      </div>
    `;
    overlay.querySelector("h3").textContent = ayuda.titulo;

    const cuerpo = overlay.querySelector(".ayuda-cuerpo");

    const resumen = document.createElement("p");
    resumen.textContent = ayuda.resumen;
    cuerpo.appendChild(resumen);

    seccion(cuerpo, "Cómo usarlo", crearLista("ol", ayuda.pasos));

    const resultado = document.createElement("p");
    resultado.textContent = ayuda.resultado;
    seccion(cuerpo, "Qué obtienes", resultado);

    if (ayuda.notas && ayuda.notas.length) {
      seccion(cuerpo, "Ten en cuenta", crearLista("ul", ayuda.notas));
    }

    document.body.appendChild(overlay);

    function cerrar() {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      if (botonOrigen) botonOrigen.focus();
    }
    function onKey(e) {
      if (e.key === "Escape" || e.key === "Enter") cerrar();
    }

    overlay.querySelector(".modal-btn-confirm").addEventListener("click", cerrar);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) cerrar();
    });
    document.addEventListener("keydown", onKey);
    overlay.querySelector(".modal-btn-confirm").focus();
  }

  function insertarBoton() {
    const h1 = document.querySelector(".topbar h1");
    if (!h1) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-ayuda";
    btn.textContent = "?";
    btn.title = "Ayuda: cómo funciona este módulo";
    btn.setAttribute("aria-label", "Ayuda: cómo funciona este módulo");
    btn.addEventListener("click", () => abrirAyuda(btn));
    h1.appendChild(btn);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", insertarBoton);
  } else {
    insertarBoton();
  }
})();