// no-programados.js — lógica de la página del módulo Verificador No Programados

const inputExcel = document.getElementById("excel");
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
  resumenEl.textContent = excel ? excel.name : "";
}

inputExcel.addEventListener("change", actualizarResumen);

btnProcesar.addEventListener("click", async () => {
  const excel = inputExcel.files[0];

  if (!excel) {
    notificar("Selecciona el Consolidado General (.xlsx).", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("excel", excel);

  btnProcesar.disabled = true;
  bloqueProgreso.classList.remove("oculto");
  barraProgreso.style.width = "0%";
  mensajeProgreso.textContent = "Subiendo Excel y buscando Excels de programación en Base de Datos…";
  resultadoEl.innerHTML = "<p class='placeholder'>Procesando…</p>";

  try {
    // Los Excels de programación no viajan en el form: el backend los toma
    // de la sección "Base de Datos" (módulo "no_programados").
    const { id_ejecucion } = await apiPost("/api/no-programados", formData);
    notificar("Procesamiento iniciado.", "info");
    iniciarPolling(id_ejecucion);
  } catch (err) {
    // Los mensajes 400 del backend ya explican qué falta (Excel válido, archivos en Base de Datos, etc.)
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

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function aLista(v) {
  if (Array.isArray(v)) return v;
  if (v && typeof v === "object") return Object.keys(v);
  return [];
}

function contar(v) {
  if (typeof v === "number") return v;
  if (Array.isArray(v)) return v.length;
  if (v && typeof v === "object") return Object.keys(v).length;
  return 0;
}

function renderResultado(idEjecucion, resultado) {
  const r = resultado || {};
  const {
    archivo_generado = "",
    total_fichas = 0,
    total_excels_bd = 0,
    total_programado = 0,
    total_parcial = 0,
    total_no_programado = 0,
    total_sin_coincidencia = 0,
    raps_por_caso,
    fichas_sin_bd = [],
    mapa_ficha_archivos = {},
    archivos_bd_omitidos = [],
    excels_fallidos_al_descargar = [],
    fichas_omitidas_estructura = [],
  } = r;

  const stat = (valor, etiqueta) =>
    `<div class="stat-box"><div class="valor">${esc(valor)}</div><div class="etiqueta">${etiqueta}</div></div>`;

  const statGridHtml = [
    stat(total_fichas, "Fichas procesadas"),
    stat(total_excels_bd, "Excels de programación"),
    stat(total_programado, "Con programación"),
    stat(total_parcial, "Programación parcial"),
    stat(total_no_programado, "Sin programación"),
    stat(total_sin_coincidencia, "Sin coincidencia"),
  ].join("");

  const nombreArchivo = archivo_generado.split(/[\\/]/).pop();
  const url = `/api/no-programados/descargar/${idEjecucion}/${encodeURIComponent(nombreArchivo)}`;

  const avisos = [];

  const sinBd = aLista(fichas_sin_bd);
  if (sinBd.length) {
    avisos.push(`Fichas sin Excel de programación en Base de Datos: ${sinBd.map(esc).join(", ")}`);
  }

  const fallidos = aLista(excels_fallidos_al_descargar);
  if (fallidos.length) {
    avisos.push(`⚠ No se pudieron descargar ${fallidos.length} Excel(s): ${fallidos.map((x) => esc(typeof x === "object" ? (x.nombre || x.archivo || JSON.stringify(x)) : x)).join(", ")}`);
  }

  const omitidos = Array.isArray(archivos_bd_omitidos) ? archivos_bd_omitidos : [];
  if (omitidos.length) {
    const det = omitidos
      .slice(0, 10)
      .map((o) => `${esc(o.archivo)}${o.hoja ? " [" + esc(o.hoja) + "]" : ""}: ${esc(o.motivo)}`)
      .join("<br>");
    avisos.push(`⚠ ${omitidos.length} archivo(s)/hoja(s) de Base de Datos omitidos por estructura no reconocida:<br>${det}`);
  }

  const hojasOmitidas = Array.isArray(fichas_omitidas_estructura) ? fichas_omitidas_estructura : [];
  if (hojasOmitidas.length) {
    avisos.push(`⚠ Hojas del Consolidado omitidas (estructura no reconocida): ${hojasOmitidas.map((h) => esc(h.ficha)).join(", ")}`);
  }

  const avisosHtml = avisos.map((t) => `<p class="placeholder">${t}</p>`).join("");

  // Detalle por ficha -> archivos usados
  const fichasMapa = Object.keys(mapa_ficha_archivos || {});
  const mapaHtml = fichasMapa.length
    ? `<details><summary>Excels usados por ficha (${fichasMapa.length})</summary><ul>${fichasMapa
        .map((f) => {
          const archivos = aLista(mapa_ficha_archivos[f]).map(esc).join(", ");
          return `<li><strong>${esc(f)}</strong>: ${archivos}</li>`;
        })
        .join("")}</ul></details>`
    : "";

  // Distribución de RAPs por caso (estructura flexible: objeto {caso: n|lista})
  let casosHtml = "";
  if (raps_por_caso && typeof raps_por_caso === "object" && !Array.isArray(raps_por_caso)) {
    const filas = Object.keys(raps_por_caso)
      .map((k) => `<li>${esc(k)}: <strong>${contar(raps_por_caso[k])}</strong></li>`)
      .join("");
    if (filas) casosHtml = `<details><summary>RAPs por caso</summary><ul>${filas}</ul></details>`;
  }

  resultadoEl.innerHTML = `
    <div class="stat-grid">${statGridHtml}</div>
    <div class="descargas"><a href="${url}" download>⬇ ${esc(nombreArchivo)}</a></div>
    ${avisosHtml}
    ${casosHtml}
    ${mapaHtml}
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
        const p = item.parametros || {};
        const r = item.resultado || {};
        const parcial = r.total_parcial ? ` / ${r.total_parcial} parcial` : "";
        const nExcels = p.n_excels ?? r.total_excels_bd;
        const excelsTxt = nExcels != null ? `, ${nExcels} Excel(s) de programación` : "";
        return `<div class="historial-item">
          <span class="fecha">${esc(item.fecha)}</span> — ${esc(p.excel)}${excelsTxt},
          ${r.total_programado ?? 0} con programación${parcial} / ${r.total_no_programado ?? 0} sin programación
        </div>`;
      })
      .join("");
  } catch (err) {
    historialEl.innerHTML = "<p class='placeholder'>No se pudo cargar el historial.</p>";
  }
}

cargarHistorial();