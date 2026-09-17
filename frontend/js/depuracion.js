// depuracion.js — lógica de la página del módulo Depuración

const inputArchivo = document.getElementById("archivo");
const nombreArchivoEl = document.getElementById("nombreArchivo");
const btnProcesar = document.getElementById("btnProcesar");
const progresoEl = document.getElementById("progreso");
const resultadoEl = document.getElementById("resultado");
const historialEl = document.getElementById("historial");

inputArchivo.addEventListener("change", () => {
  nombreArchivoEl.textContent = inputArchivo.files[0]
    ? `Seleccionado: ${inputArchivo.files[0].name}`
    : "";
});

btnProcesar.addEventListener("click", async () => {
  const archivo = inputArchivo.files[0];
  const limite = document.getElementById("limite").value;

  if (!archivo) {
    notificar("Selecciona un archivo .xlsx.", "warning");
    return;
  }

  const formData = new FormData();
  formData.append("limite", limite);
  formData.append("archivo", archivo);

  btnProcesar.disabled = true;
  progresoEl.classList.remove("oculto");

  try {
    const resultado = await apiPost("/api/depuracion", formData);
    renderResultado(resultado);
    notificar("Procesamiento completado.", "success");
    cargarHistorial();
  } catch (err) {
    notificar(err.message || "Ocurrió un error al procesar el archivo.", "error");
  } finally {
    btnProcesar.disabled = false;
    progresoEl.classList.add("oculto");
  }
});

function renderResultado(resultado) {
  const { id_ejecucion, limite, total_hojas_marcadas, total_personas_marcadas, detalle_hojas, archivo_generado } = resultado;

  const statGridHtml = `
    <div class="stat-box"><div class="valor">${limite}</div><div class="etiqueta">Límite usado</div></div>
    <div class="stat-box"><div class="valor">${total_hojas_marcadas}</div><div class="etiqueta">Hojas marcadas</div></div>
    <div class="stat-box"><div class="valor">${total_personas_marcadas}</div><div class="etiqueta">Personas marcadas</div></div>
  `;

  const nombreArchivo = archivo_generado.split(/[\\/]/).pop();
  const url = `/api/depuracion/descargar/${id_ejecucion}/${encodeURIComponent(nombreArchivo)}`;

  const filasTabla = detalle_hojas
    .map(
      (h) => `<tr><td>${h.hoja}</td><td>${h.marcada ? "Sí" : "No"}</td><td>${h.personas_marcadas}</td></tr>`
    )
    .join("");

  resultadoEl.innerHTML = `
    <div class="stat-grid">${statGridHtml}</div>
    <div class="descargas"><a href="${url}" download>⬇ ${nombreArchivo}</a></div>
    <table class="tabla-fichas">
      <thead><tr><th>Hoja</th><th>Marcada</th><th>Personas marcadas</th></tr></thead>
      <tbody>${filasTabla}</tbody>
    </table>
  `;
}

async function cargarHistorial() {
  try {
    const items = await apiGet("/api/depuracion/historial?limite=10");
    if (!items.length) {
      historialEl.innerHTML = "<p class='placeholder'>Aún no hay ejecuciones registradas.</p>";
      return;
    }
    historialEl.innerHTML = items
      .map((item) => {
        const p = item.parametros;
        const r = item.resultado;
        return `<div class="historial-item">
          <span class="fecha">${item.fecha}</span> — ${p.archivo},
          límite ${p.limite}, ${r.total_hojas_marcadas} hoja(s) marcada(s)
        </div>`;
      })
      .join("");
  } catch (err) {
    historialEl.innerHTML = "<p class='placeholder'>No se pudo cargar el historial.</p>";
  }
}

cargarHistorial();