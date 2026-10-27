// no-aprobados.js — lógica de la página del módulo Consolidador NoAprobados

const inputArchivos = document.getElementById("archivos");
const listaArchivosEl = document.getElementById("listaArchivos");
const btnProcesar = document.getElementById("btnProcesar");
const progresoEl = document.getElementById("progreso");
const resultadoEl = document.getElementById("resultado");
const historialEl = document.getElementById("historial");

inputArchivos.addEventListener("change", () => {
  const archivos = Array.from(inputArchivos.files);
  if (archivos.length === 0) {
    listaArchivosEl.textContent = "";
    return;
  }
  listaArchivosEl.textContent =
    `${archivos.length} archivo(s) seleccionados: ` +
    archivos.map((a) => a.name).join(", ");
});

btnProcesar.addEventListener("click", async () => {
  const archivos = inputArchivos.files;
  const filtro = document.getElementById("filtro").value;
  const generarGeneral = document.getElementById("generarGeneral").checked;

  if (!archivos || archivos.length === 0) {
    notificar("Selecciona al menos un archivo .xls o .xlsx.", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("filtro", filtro);
  formData.append("generar_general", generarGeneral);
  Array.from(archivos).forEach((a) => formData.append("archivos", a));

  btnProcesar.disabled = true;
  progresoEl.classList.remove("oculto");

  try {
    const resultado = await apiPost("/api/consolidador", formData);
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
  const { id_ejecucion, filtro, archivos_procesados, stats, archivos_generados } = resultado;

  const statBoxes = [
    { etiqueta: "Archivos procesados", valor: archivos_procesados },
    { etiqueta: "Fichas Ramos", valor: stats.fichas.RAMOS },
    { etiqueta: "Fichas Gelves", valor: stats.fichas.GELVES },
    { etiqueta: `${filtro} en Ramos`, valor: stats.coincidencias.RAMOS },
    { etiqueta: `${filtro} en Gelves`, valor: stats.coincidencias.GELVES },
    { etiqueta: "No reconocidos", valor: stats.no_reconocidos.length },
    { etiqueta: "Errores", valor: stats.errores.length },
  ];

  const statGridHtml = statBoxes
    .map(
      (s) => `<div class="stat-box"><div class="valor">${s.valor}</div><div class="etiqueta">${s.etiqueta}</div></div>`
    )
    .join("");

  const descargasHtml = Object.entries(archivos_generados)
    .map(([carpeta, ruta]) => {
      const nombreArchivo = ruta.split(/[\\/]/).pop();
      const url = `/api/consolidador/descargar/${id_ejecucion}/${encodeURIComponent(nombreArchivo)}`;
      return `<a href="${url}" download>⬇ ${nombreArchivo}</a>`;
    })
    .join("");

  const filasTabla = stats.detalle_fichas
    .map(
      (f) => `<tr><td>${f.ficha}</td><td>${f.programa}</td><td>${f.carpeta}</td><td>${f.coincidencias}</td></tr>`
    )
    .join("");

  const tablaHtml = stats.detalle_fichas.length
    ? `<table class="tabla-fichas">
        <thead><tr><th>Ficha</th><th>Programa</th><th>Carpeta</th><th>${filtro}</th></tr></thead>
        <tbody>${filasTabla}</tbody>
      </table>`
    : "";

  resultadoEl.innerHTML = `
    <div class="stat-grid">${statGridHtml}</div>
    <div class="descargas">${descargasHtml || "<span class='placeholder'>No se generó ningún archivo.</span>"}</div>
    ${tablaHtml}
  `;
}

async function cargarHistorial() {
  try {
    const items = await apiGet("/api/consolidador/historial?limite=10");
    if (!items.length) {
      historialEl.innerHTML = "<p class='placeholder'>Aún no hay ejecuciones registradas.</p>";
      return;
    }
    historialEl.innerHTML = items
      .map((item) => {
        const p = item.parametros;
        const s = item.resultado;
        const totalFichas = (s.fichas?.RAMOS || 0) + (s.fichas?.GELVES || 0);
        return `<div class="historial-item">
          <span class="fecha">${item.fecha}</span> — filtro <b>${p.filtro}</b>,
          ${p.n_archivos} archivo(s), ${totalFichas} ficha(s) reconocidas
        </div>`;
      })
      .join("");
  } catch (err) {
    historialEl.innerHTML = "<p class='placeholder'>No se pudo cargar el historial.</p>";
  }
}

cargarHistorial();
