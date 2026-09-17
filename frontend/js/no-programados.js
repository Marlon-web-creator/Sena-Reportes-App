// no-programados.js — lógica de la página del módulo Verificador No Programados

const inputExcel = document.getElementById("excel");
const inputPdfs = document.getElementById("pdfs");
const resumenEl = document.getElementById("resumenSeleccion");
const btnProcesar = document.getElementById("btnProcesar");
const bloqueProgreso = document.getElementById("bloqueProgreso");
const barraProgreso = document.getElementById("barraProgreso");
const mensajeProgreso = document.getElementById("mensajeProgreso");
const resultadoEl = document.getElementById("resultado");
const historialEl = document.getElementById("historial");

let intervaloPolling = null;

function actualizarResumen() {
  const excel = inputExcel.files[0];
  const pdfs = Array.from(inputPdfs.files).filter((f) => f.name.toLowerCase().endsWith(".pdf"));
  resumenEl.textContent = excel
    ? `${excel.name} · ${pdfs.length} PDF(s) detectados en la carpeta`
    : "";
}

inputExcel.addEventListener("change", actualizarResumen);
inputPdfs.addEventListener("change", actualizarResumen);

btnProcesar.addEventListener("click", async () => {
  const excel = inputExcel.files[0];
  const pdfs = Array.from(inputPdfs.files).filter((f) => f.name.toLowerCase().endsWith(".pdf"));

  if (!excel) {
    notificar("Selecciona el Consolidado General (.xlsx).", "warning");
    return;
  }
  if (pdfs.length === 0) {
    notificar("Selecciona la carpeta BD con al menos un PDF.", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("excel", excel);
  // webkitRelativePath conserva la subcarpeta (ej. "BD/2904878/reporte.pdf"),
  // que es justo lo que el backend usa para relacionar cada PDF con su ficha.
  pdfs.forEach((pdf) => {
    const rutaRelativa = pdf.webkitRelativePath || pdf.name;
    formData.append("pdfs", pdf, rutaRelativa);
  });

  btnProcesar.disabled = true;
  bloqueProgreso.classList.remove("oculto");
  barraProgreso.style.width = "0%";
  mensajeProgreso.textContent = "Subiendo archivos…";
  resultadoEl.innerHTML = "<p class='placeholder'>Procesando…</p>";

  try {
    const { id_ejecucion } = await apiPost("/api/no-programados", formData);
    notificar("Procesamiento iniciado.", "info");
    iniciarPolling(id_ejecucion);
  } catch (err) {
    notificar(err.message || "No se pudo iniciar el procesamiento.", "error");
    btnProcesar.disabled = false;
    bloqueProgreso.classList.add("oculto");
  }
});

function iniciarPolling(idEjecucion) {
  if (intervaloPolling) clearInterval(intervaloPolling);

  intervaloPolling = setInterval(async () => {
    try {
      const estado = await apiGet(`/api/no-programados/progreso/${idEjecucion}`);

      const porcentaje = estado.total > 0 ? Math.round((estado.actual / estado.total) * 100) : 5;
      barraProgreso.style.width = `${Math.max(porcentaje, 5)}%`;
      mensajeProgreso.textContent = estado.mensaje || "Procesando…";

      if (estado.estado === "listo") {
        clearInterval(intervaloPolling);
        barraProgreso.style.width = "100%";
        btnProcesar.disabled = false;
        renderResultado(idEjecucion, estado.resultado);
        notificar("Procesamiento completado.", "success");
        cargarHistorial();
      } else if (estado.estado === "error") {
        clearInterval(intervaloPolling);
        btnProcesar.disabled = false;
        resultadoEl.innerHTML = "<p class='placeholder'>El procesamiento falló.</p>";
        notificar(estado.error || "Ocurrió un error durante el procesamiento.", "error");
      }
    } catch (err) {
      clearInterval(intervaloPolling);
      btnProcesar.disabled = false;
      notificar("Se perdió la conexión con el servidor.", "error");
    }
  }, 1500);
}

function renderResultado(idEjecucion, resultado) {
  const {
    archivo_generado, ocr_disponible, total_fichas, total_pdfs_detectados,
    fichas_sin_pdf, total_programado, total_no_programado,
  } = resultado;

  const statGridHtml = `
    <div class="stat-box"><div class="valor">${total_fichas}</div><div class="etiqueta">Fichas procesadas</div></div>
    <div class="stat-box"><div class="valor">${total_pdfs_detectados}</div><div class="etiqueta">PDFs detectados</div></div>
    <div class="stat-box"><div class="valor">${total_programado}</div><div class="etiqueta">Programado</div></div>
    <div class="stat-box"><div class="valor">${total_no_programado}</div><div class="etiqueta">No programado</div></div>
  `;

  const nombreArchivo = archivo_generado.split(/[\\/]/).pop();
  const url = `/api/no-programados/descargar/${idEjecucion}/${encodeURIComponent(nombreArchivo)}`;

  const avisoOcr = ocr_disponible
    ? ""
    : `<p class="placeholder">⚠ OCR no disponible en el servidor: solo se leyeron PDFs con texto digital.</p>`;

  const avisoSinPdf = fichas_sin_pdf.length
    ? `<p class="placeholder">Fichas sin PDF asociado: ${fichas_sin_pdf.join(", ")}</p>`
    : "";

  resultadoEl.innerHTML = `
    <div class="stat-grid">${statGridHtml}</div>
    <div class="descargas"><a href="${url}" download>⬇ ${nombreArchivo}</a></div>
    ${avisoOcr}
    ${avisoSinPdf}
  `;
}

async function cargarHistorial() {
  try {
    const items = await apiGet("/api/no-programados/historial?limite=10");
    if (!items.length) {
      historialEl.innerHTML = "<p class='placeholder'>Aún no hay ejecuciones registradas.</p>";
      return;
    }
    historialEl.innerHTML = items
      .map((item) => {
        const p = item.parametros;
        const r = item.resultado;
        return `<div class="historial-item">
          <span class="fecha">${item.fecha}</span> — ${p.excel},
          ${r.total_programado} programado / ${r.total_no_programado} no programado
        </div>`;
      })
      .join("");
  } catch (err) {
    historialEl.innerHTML = "<p class='placeholder'>No se pudo cargar el historial.</p>";
  }
}

cargarHistorial();