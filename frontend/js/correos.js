// correos.js — lógica de la página del módulo Correo de Aprendices
// El correo se genera a partir de nombres, apellidos y documento del consolidado
// (ya no se suben archivos xls con correos).

const inputConsolidado = document.getElementById("consolidado");
const inputPlantilla = document.getElementById("plantilla");
const inputDominio = document.getElementById("dominio");
const inputFilaEncabezado = document.getElementById("filaEncabezado");
const inputColDocumento = document.getElementById("colDocumento");
const resumenEl = document.getElementById("resumenSeleccion");
const vistaPreviaEl = document.getElementById("vistaPrevia");
const btnProcesar = document.getElementById("btnProcesar");
const progresoEl = document.getElementById("progreso");
const resultadoEl = document.getElementById("resultado");
const historialEl = document.getElementById("historial");

const PLANTILLA_DEFECTO = "{inicial}{apellido1}{doc2}";
const DOMINIO_DEFECTO = "soy.sena.edu.co";

function escapar(texto) {
  return String(texto ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function actualizarResumen() {
  const consolidado = inputConsolidado.files[0];
  resumenEl.textContent = consolidado ? consolidado.name : "";
}

// Vista previa aproximada de la plantilla con un aprendiz de ejemplo.
// (La generación real y su validación se hacen en el servidor.)
function actualizarVistaPrevia() {
  const plantilla = inputPlantilla.value.trim() || PLANTILLA_DEFECTO;
  const dominio = (inputDominio.value.trim() || DOMINIO_DEFECTO).replace(/^@/, "");
  const doc = "1000239293";
  const variables = {
    nombre1: "chrisbel", nombre2: "yessenia", inicial: "c", iniciales: "cy",
    apellido1: "choconta", apellido2: "rojas",
    doc, doc2: doc.slice(-2), doc3: doc.slice(-3), doc4: doc.slice(-4),
  };
  let desconocida = null;
  const usuario = plantilla.replace(/\{(\w+)\}/g, (_, clave) => {
    if (!(clave in variables)) { desconocida = clave; return ""; }
    return variables[clave];
  });
  vistaPreviaEl.textContent = desconocida
    ? `⚠ Variable desconocida: {${desconocida}}`
    : `Ejemplo (CHRISBEL YESSENIA CHOCONTA ROJAS): ${usuario}@${dominio}`;
}

inputConsolidado.addEventListener("change", actualizarResumen);
inputPlantilla.addEventListener("input", actualizarVistaPrevia);
inputDominio.addEventListener("input", actualizarVistaPrevia);

btnProcesar.addEventListener("click", async () => {
  const consolidado = inputConsolidado.files[0];

  if (!consolidado) {
    notificar("Selecciona el archivo consolidado.", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("consolidado", consolidado);
  formData.append("plantilla", inputPlantilla.value.trim() || PLANTILLA_DEFECTO);
  formData.append("dominio", inputDominio.value.trim() || DOMINIO_DEFECTO);
  // Opcionales: si se dejan vacíos, el servidor los detecta por los encabezados.
  if (inputFilaEncabezado.value) formData.append("fila_encabezado", inputFilaEncabezado.value);
  if (inputColDocumento.value) formData.append("col_documento", inputColDocumento.value);

  btnProcesar.disabled = true;
  progresoEl.classList.remove("oculto");

  try {
    const resultado = await apiPost("/api/correos", formData);
    renderResultado(resultado);
    notificar("Procesamiento completado.", "success");
    cargarHistorial();
  } catch (err) {
    notificar(err.message || "Ocurrió un error al procesar el consolidado.", "error");
  } finally {
    btnProcesar.disabled = false;
    progresoEl.classList.add("oculto");
  }
});

function renderResultado(resultado) {
  const {
    id_ejecucion, archivo_generado, plantilla_usada,
    total_aprendices, total_documentos_consolidado, total_encontrados,
    total_sin_correo, documentos_sin_correo = [],
    correos_con_colision = [], hojas_omitidas = [],
  } = resultado;

  const statGridHtml = `
    <div class="stat-box"><div class="valor">${total_aprendices}</div><div class="etiqueta">Aprendices</div></div>
    <div class="stat-box"><div class="valor">${total_documentos_consolidado}</div><div class="etiqueta">Filas en consolidado</div></div>
    <div class="stat-box"><div class="valor">${total_encontrados}</div><div class="etiqueta">Filas con correo</div></div>
    <div class="stat-box"><div class="valor">${total_sin_correo}</div><div class="etiqueta">Sin correo</div></div>
  `;

  const nombreArchivo = archivo_generado.split(/[\\/]/).pop();
  const url = `/api/correos/descargar/${id_ejecucion}/${encodeURIComponent(nombreArchivo)}`;

  const avisoPlantilla = plantilla_usada
    ? `<p class="placeholder">Formato aplicado: ${escapar(plantilla_usada)}</p>`
    : "";

  const avisoSinCorreo = documentos_sin_correo.length
    ? `<p class="placeholder">⚠ Documentos sin nombre/apellido para generar el correo (primeros ${documentos_sin_correo.length}): ${documentos_sin_correo.map(escapar).join(", ")}</p>`
    : "";

  const avisoColisiones = correos_con_colision.length
    ? `<p class="placeholder">⚠ Correos repetidos entre aprendices distintos (se les agregó un número; revísalos):<br>${
        correos_con_colision
          .map((c) => `${escapar(c.documento_nuevo)} → ${escapar(c.correo_asignado)} (coincidía con ${escapar(c.documento_previo)})`)
          .join("<br>")
      }</p>`
    : "";

  const avisoHojas = hojas_omitidas.length
    ? `<p class="placeholder">⚠ Hojas omitidas por no tener las columnas Número de Documento / Nombres / Apellidos: ${hojas_omitidas.map(escapar).join(", ")}</p>`
    : "";

  resultadoEl.innerHTML = `
    <div class="stat-grid">${statGridHtml}</div>
    <div class="descargas"><a href="${url}" download>⬇ ${escapar(nombreArchivo)}</a></div>
    ${avisoPlantilla}
    ${avisoSinCorreo}
    ${avisoColisiones}
    ${avisoHojas}
  `;
}

async function cargarHistorial() {
  try {
    const items = await apiGet("/api/correos/historial?limite=10");
    if (!items.length) {
      historialEl.innerHTML = "<p class='placeholder'>Aún no hay ejecuciones registradas.</p>";
      return;
    }
    historialEl.innerHTML = items
      .map((item) => {
        const p = item.parametros || {};
        const r = item.resultado || {};
        // Las ejecuciones antiguas (con xls) conservan estas mismas claves.
        return `<div class="historial-item">
          <span class="fecha">${escapar(item.fecha)}</span> — ${escapar(p.consolidado)},
          ${escapar(r.total_encontrados)}/${escapar(r.total_documentos_consolidado)} filas con correo
        </div>`;
      })
      .join("");
  } catch (err) {
    historialEl.innerHTML = "<p class='placeholder'>No se pudo cargar el historial.</p>";
  }
}

actualizarVistaPrevia();
cargarHistorial();