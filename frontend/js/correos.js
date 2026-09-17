// correos.js — lógica de la página del módulo Correo de Aprendices

const inputConsolidado = document.getElementById("consolidado");
const inputXls = document.getElementById("archivosXls");
const resumenEl = document.getElementById("resumenSeleccion");
const btnProcesar = document.getElementById("btnProcesar");
const progresoEl = document.getElementById("progreso");
const resultadoEl = document.getElementById("resultado");
const historialEl = document.getElementById("historial");

function actualizarResumen() {
  const consolidado = inputConsolidado.files[0];
  const xls = Array.from(inputXls.files);
  resumenEl.textContent = consolidado
    ? `${consolidado.name} · ${xls.length} archivo(s) de correos seleccionados`
    : "";
}

inputConsolidado.addEventListener("change", actualizarResumen);
inputXls.addEventListener("change", actualizarResumen);

btnProcesar.addEventListener("click", async () => {
  const consolidado = inputConsolidado.files[0];
  const archivosXls = Array.from(inputXls.files);

  if (!consolidado) {
    notificar("Selecciona el archivo consolidado.", "warning");
    return;
  }
  if (archivosXls.length === 0) {
    notificar("Selecciona al menos un archivo xls con los correos.", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("consolidado", consolidado);
  archivosXls.forEach((a) => formData.append("archivos_xls", a));
  formData.append("fila_inicio_consolidado", document.getElementById("filaInicioConsolidado").value);
  formData.append("col_documento_consolidado", document.getElementById("colDocumentoConsolidado").value);
  formData.append("fila_inicio_xls", document.getElementById("filaInicioXls").value);

  btnProcesar.disabled = true;
  progresoEl.classList.remove("oculto");

  try {
    const resultado = await apiPost("/api/correos", formData);
    renderResultado(resultado);
    notificar("Procesamiento completado.", "success");
    cargarHistorial();
  } catch (err) {
    notificar(err.message || "Ocurrió un error al procesar los archivos.", "error");
  } finally {
    btnProcesar.disabled = false;
    progresoEl.classList.add("oculto");
  }
});

function renderResultado(resultado) {
  const {
    id_ejecucion, archivo_generado, total_correos_indexados,
    total_documentos_consolidado, total_encontrados, total_sin_correo,
    documentos_sin_correo, documentos_con_correos_distintos,
  } = resultado;

  const statGridHtml = `
    <div class="stat-box"><div class="valor">${total_correos_indexados}</div><div class="etiqueta">Correos indexados</div></div>
    <div class="stat-box"><div class="valor">${total_documentos_consolidado}</div><div class="etiqueta">Documentos en consolidado</div></div>
    <div class="stat-box"><div class="valor">${total_encontrados}</div><div class="etiqueta">Correos encontrados</div></div>
    <div class="stat-box"><div class="valor">${total_sin_correo}</div><div class="etiqueta">Sin correo</div></div>
  `;

  const nombreArchivo = archivo_generado.split(/[\\/]/).pop();
  const url = `/api/correos/descargar/${id_ejecucion}/${encodeURIComponent(nombreArchivo)}`;

  const avisoSinCorreo = documentos_sin_correo.length
    ? `<p class="placeholder">Documentos sin correo (primeros ${documentos_sin_correo.length}): ${documentos_sin_correo.join(", ")}</p>`
    : "";

  const claves = Object.keys(documentos_con_correos_distintos || {});
  const avisoDuplicados = claves.length
    ? `<p class="placeholder">⚠ Documentos con más de un correo distinto en los archivos fuente: ${claves.join(", ")}</p>`
    : "";

  resultadoEl.innerHTML = `
    <div class="stat-grid">${statGridHtml}</div>
    <div class="descargas"><a href="${url}" download>⬇ ${nombreArchivo}</a></div>
    ${avisoSinCorreo}
    ${avisoDuplicados}
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
        const p = item.parametros;
        const r = item.resultado;
        return `<div class="historial-item">
          <span class="fecha">${item.fecha}</span> — ${p.consolidado},
          ${r.total_encontrados}/${r.total_documentos_consolidado} correos encontrados
        </div>`;
      })
      .join("");
  } catch (err) {
    historialEl.innerHTML = "<p class='placeholder'>No se pudo cargar el historial.</p>";
  }
}

cargarHistorial();