// horas-instructores.js — lógica de la página del módulo Comparador de Horas Instructores

const selectMes = document.getElementById("mes");
const inputReporte = document.getElementById("reporte");
const resumenEl = document.getElementById("resumenSeleccion");
const btnProcesar = document.getElementById("btnProcesar");
const bloqueProgreso = document.getElementById("bloqueProgreso");
const barraProgreso = document.getElementById("barraProgreso");
const mensajeProgreso = document.getElementById("mensajeProgreso");
const resultadoEl = document.getElementById("resultado");
const historialEl = document.getElementById("historial");

let intervaloPolling = null;

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function actualizarResumen() {
  const reporte = inputReporte.files[0];
  resumenEl.textContent = reporte ? reporte.name : "";
}

inputReporte.addEventListener("change", actualizarResumen);

btnProcesar.addEventListener("click", async () => {
  const reporte = inputReporte.files[0];
  const mes = selectMes.value;

  if (!mes) {
    notificar("Selecciona el mes a comparar.", "warning");
    return;
  }
  if (!reporte) {
    notificar("Selecciona el Reporte Ejecución Horas Instructor.", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("reporte", reporte);
  formData.append("mes", mes);

  btnProcesar.disabled = true;
  bloqueProgreso.classList.remove("oculto");
  barraProgreso.style.width = "0%";
  mensajeProgreso.textContent = "Subiendo el reporte y buscando los Excels de horas en Base de Datos…";
  resultadoEl.innerHTML = "<p class='placeholder'>Procesando…</p>";

  try {
    // Los Excels de horas no viajan en el form: el backend los toma de la
    // sección "Base de Datos" (módulo "horas_instructores").
    const { id_ejecucion } = await apiPost("/api/horas-instructores", formData);
    notificar("Comparación iniciada.", "info");
    iniciarPolling(id_ejecucion);
  } catch (err) {
    notificar(err.message || "No se pudo iniciar la comparación.", "error");
    btnProcesar.disabled = false;
    bloqueProgreso.classList.add("oculto");
  }
});

function iniciarPolling(idEjecucion) {
  if (intervaloPolling) clearInterval(intervaloPolling);

  intervaloPolling = setInterval(async () => {
    try {
      const estado = await apiGet(`/api/horas-instructores/progreso/${idEjecucion}`);

      const porcentaje = estado.total > 0 ? Math.round((estado.actual / estado.total) * 100) : 5;
      barraProgreso.style.width = `${Math.max(porcentaje, 5)}%`;
      mensajeProgreso.textContent = estado.mensaje || "Procesando…";

      if (estado.estado === "listo") {
        clearInterval(intervaloPolling);
        barraProgreso.style.width = "100%";
        btnProcesar.disabled = false;
        renderResultado(idEjecucion, estado.resultado);
        notificar("Comparación completada.", "success");
        cargarHistorial();
      } else if (estado.estado === "error") {
        clearInterval(intervaloPolling);
        btnProcesar.disabled = false;
        resultadoEl.innerHTML = "<p class='placeholder'>La comparación falló.</p>";
        notificar(estado.error || "Ocurrió un error durante la comparación.", "error");
      }
    } catch (err) {
      clearInterval(intervaloPolling);
      btnProcesar.disabled = false;
      notificar("Se perdió la conexión con el servidor.", "error");
    }
  }, 1500);
}

function renderResultado(idEjecucion, resultado) {
  const r = resultado || {};
  const {
    archivo_generado = "",
    mes_nombre = "",
    total_instructores_reporte = 0,
    total_instructores_bd = 0,
    total_coinciden = 0,
    total_reporte_mayor = 0,
    total_reporte_menor = 0,
    total_solo_reporte = 0,
    total_solo_bd = 0,
    horas_titulada_reporte = 0,
    horas_grupos_bd = 0,
    solo_reporte_por_vinculacion = {},
    archivos_bd_usados = [],
    archivos_bd_omitidos = [],
    documentos_en_varios_archivos = [],
    documentos_repetidos_en_reporte = [],
    tipos_no_reconocidos = [],
    excels_fallidos_al_descargar = [],
  } = r;

  const stat = (valor, etiqueta) =>
    `<div class="stat-box"><div class="valor">${esc(valor)}</div><div class="etiqueta">${etiqueta}</div></div>`;

  const statGridHtml = [
    stat(total_instructores_reporte, "Instructores en el reporte"),
    stat(total_instructores_bd, `Instructores en la BD (${esc(mes_nombre)})`),
    stat(total_coinciden, "Coinciden"),
    stat(total_reporte_mayor, "Reporte > BD"),
    stat(total_reporte_menor, "Reporte < BD"),
    stat(total_solo_reporte, "Solo en reporte"),
    stat(total_solo_bd, "Solo en BD"),
  ].join("");

  const nombreArchivo = archivo_generado.split(/[\\/]/).pop();
  const url = `/api/horas-instructores/descargar/${idEjecucion}/${encodeURIComponent(nombreArchivo)}`;

  const horasHtml = `<p class="placeholder">Horas de formación titulada en el reporte: <strong>${esc(horas_titulada_reporte)}</strong> · Horas asociadas a grupos en la BD (${esc(mes_nombre)}): <strong>${esc(horas_grupos_bd)}</strong></p>`;

  const avisos = [];

  if (excels_fallidos_al_descargar.length) {
    avisos.push(`⚠ No se pudieron descargar ${excels_fallidos_al_descargar.length} Excel(s): ${excels_fallidos_al_descargar.map(esc).join(", ")}`);
  }
  if (archivos_bd_omitidos.length) {
    const det = archivos_bd_omitidos
      .slice(0, 10)
      .map((o) => `${esc(o.archivo)}${o.hoja ? " [" + esc(o.hoja) + "]" : ""}: ${esc(o.motivo)}`)
      .join("<br>");
    avisos.push(`⚠ ${archivos_bd_omitidos.length} archivo(s)/hoja(s) de Base de Datos omitidos:<br>${det}`);
  }
  if (documentos_en_varios_archivos.length) {
    avisos.push(`⚠ Documentos presentes en varios Excels de la BD (se sumaron sus horas): ${documentos_en_varios_archivos.map(esc).join(", ")}`);
  }
  if (documentos_repetidos_en_reporte.length) {
    avisos.push(`⚠ Documentos repetidos en el reporte (se sumaron sus horas): ${documentos_repetidos_en_reporte.map(esc).join(", ")}`);
  }
  if (tipos_no_reconocidos.length) {
    avisos.push(`⚠ Tipos de hora de la BD no reconocidos (ignorados): ${tipos_no_reconocidos.map(esc).join(", ")}`);
  }
  const avisosHtml = avisos.map((t) => `<p class="placeholder">${t}</p>`).join("");

  const vinc = Object.keys(solo_reporte_por_vinculacion);
  const vincHtml = vinc.length
    ? `<details><summary>Solo en reporte, por tipo de vinculación</summary><ul>${vinc
        .map((k) => `<li>${esc(k)}: <strong>${esc(solo_reporte_por_vinculacion[k])}</strong></li>`)
        .join("")}</ul></details>`
    : "";

  const usadosHtml = archivos_bd_usados.length
    ? `<details><summary>Excels de la BD usados (${archivos_bd_usados.length})</summary><ul>${archivos_bd_usados
        .map((a) => `<li>${esc(a)}</li>`)
        .join("")}</ul></details>`
    : "";

  resultadoEl.innerHTML = `
    <div class="stat-grid">${statGridHtml}</div>
    <div class="descargas"><a href="${url}" download>⬇ ${esc(nombreArchivo)}</a></div>
    ${horasHtml}
    ${avisosHtml}
    ${vincHtml}
    ${usadosHtml}
  `;
}

async function cargarHistorial() {
  try {
    const items = await apiGet("/api/horas-instructores/historial?limite=10");
    if (!items.length) {
      historialEl.innerHTML = "<p class='placeholder'>Aún no hay ejecuciones registradas.</p>";
      return;
    }
    historialEl.innerHTML = items
      .map((item) => {
        const p = item.parametros || {};
        const r = item.resultado || {};
        const dif = (r.total_reporte_mayor ?? 0) + (r.total_reporte_menor ?? 0);
        return `<div class="historial-item">
          <span class="fecha">${esc(item.fecha)}</span> — ${esc(p.reporte)} (${esc(p.mes_nombre || p.mes)}):
          ${r.total_coinciden ?? 0} coinciden / ${dif} con diferencia / ${r.total_solo_reporte ?? 0} solo en reporte / ${r.total_solo_bd ?? 0} solo en BD
        </div>`;
      })
      .join("");
  } catch (err) {
    historialEl.innerHTML = "<p class='placeholder'>No se pudo cargar el historial.</p>";
  }
}

cargarHistorial();
