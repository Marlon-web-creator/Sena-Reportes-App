// juicios-practica.js — lógica de la pantalla "Juicios posteriores a Etapa Práctica"
// Depende de api.js (apiPost, API_BASE). Toastify es opcional.

const ENDPOINT_JUICIOS = "/api/juicios-practica";

const $ = (id) => document.getElementById(id);

// Evita inyectar HTML con datos del reporte (nombres, advertencias...).
function esc(valor) {
  const div = document.createElement("div");
  div.textContent = valor == null ? "" : String(valor);
  return div.innerHTML;
}

function setEstado(texto, esError = false) {
  const el = $("estado");
  el.textContent = texto;
  el.style.color = esError ? "crimson" : "";
}

function avisar(texto, esError = false) {
  if (typeof Toastify === "undefined") return;
  Toastify({
    text: texto,
    duration: esError ? 5000 : 3000,
    gravity: "top",
    position: "right",
    style: { background: esError ? "#c0392b" : "#2e8b57" },
  }).showToast();
}

// Muestra los archivos elegidos.
$("reportes").addEventListener("change", (e) => {
  const lista = $("lista-archivos");
  lista.innerHTML = "";
  for (const f of e.target.files) {
    const li = document.createElement("li");
    li.textContent = f.name;
    lista.appendChild(li);
  }
});

$("form-juicios").addEventListener("submit", async (e) => {
  e.preventDefault();

  const archivos = $("reportes").files;
  if (!archivos.length) {
    setEstado("Selecciona al menos un reporte.", true);
    return;
  }

  const formData = new FormData();
  for (const f of archivos) formData.append("reportes", f);

  const soloFecha = $("solo-fecha").checked;

  $("btn-ejecutar").disabled = true;
  $("seccion-resultados").hidden = true;
  setEstado("Procesando... esto puede tardar unos segundos.");

  try {
    const datos = await apiPost(`${ENDPOINT_JUICIOS}?solo_fecha=${soloFecha}`, formData);
    pintarResultado(datos);
    setEstado("Listo.");
    avisar("Verificación completada");
  } catch (err) {
    setEstado("Error: " + err.message, true);
    avisar(err.message, true);
  } finally {
    $("btn-ejecutar").disabled = false;
  }
});

function pintarResultado(r) {
  // archivo_generado es la ruta dentro del bucket
  // (generados/<modulo>/<id>/<nombre>); la descarga solo necesita
  // id_ejecucion + nombre del archivo.
  const nombreArchivo = r.archivo_generado.split("/").pop();
  $("enlace-descarga").href =
    `${API_BASE}${ENDPOINT_JUICIOS}/descargar/${encodeURIComponent(r.id_ejecucion)}/${encodeURIComponent(nombreArchivo)}`;

  // Resumen general
  $("resumen-general").innerHTML = `
    <li>Criterio de comparación: <b>${esc(r.criterio_comparacion)}</b></li>
    <li>Fichas analizadas: <b>${esc(r.total_fichas)}</b></li>
    <li>Aprendices en los reportes: <b>${esc(r.total_aprendices)}</b></li>
    <li>Con práctica aprobada: <b>${esc(r.total_con_practica_aprobada)}</b></li>
    <li>Aprendices con inconsistencia: <b>${esc(r.total_aprendices_con_inconsistencia)}</b></li>
    <li>Juicios aprobados posteriores a la práctica: <b>${esc(r.total_juicios_posteriores)}</b></li>
  `;

  // Resumen por ficha
  $("tabla-fichas").querySelector("tbody").innerHTML = (r.resumen_por_ficha || [])
    .map(
      (f) => `<tr>
        <td>${esc(f.ficha)}</td>
        <td>${esc(f.aprendices)}</td>
        <td>${esc(f.con_practica_aprobada)}</td>
        <td>${esc(f.con_inconsistencia)}</td>
        <td>${esc(f.juicios_posteriores)}</td>
      </tr>`
    )
    .join("");

  // Aprendices a revisar (el backend devuelve máximo 50)
  const aprendices = r.aprendices_con_inconsistencia || [];
  $("tabla-aprendices").querySelector("tbody").innerHTML = aprendices.length
    ? aprendices
        .map(
          (a) => `<tr>
            <td>${esc(a.ficha)}</td>
            <td>${esc(a.documento)}</td>
            <td>${esc(a.nombre)}</td>
            <td>${esc(a.estado)}</td>
            <td>${esc(a.fecha_practica)}</td>
            <td>${esc(a.juicios_posteriores)}</td>
            <td>${esc(a.ultimo_juicio_posterior)}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="7">No se encontraron inconsistencias 🎉</td></tr>`;

  $("nota-limite").textContent =
    r.total_aprendices_con_inconsistencia > aprendices.length
      ? `(mostrando ${aprendices.length} de ${r.total_aprendices_con_inconsistencia}; el Excel trae todos)`
      : "";

  // Fichas sin práctica aprobada
  const sinPractica = r.fichas_sin_practica_aprobada || [];
  $("bloque-sin-practica").hidden = !sinPractica.length;
  $("lista-sin-practica").innerHTML = sinPractica.map((f) => `<li>${esc(f)}</li>`).join("");

  // Advertencias
  const adv = r.advertencias || [];
  $("bloque-advertencias").hidden = !adv.length;
  $("lista-advertencias").innerHTML = adv.map((a) => `<li>${esc(a)}</li>`).join("");

  // Detalle técnico
  $("columnas-detectadas").textContent = JSON.stringify(r.columnas_detectadas || [], null, 2);

  $("seccion-resultados").hidden = false;
  $("seccion-resultados").scrollIntoView({ behavior: "smooth" });
}