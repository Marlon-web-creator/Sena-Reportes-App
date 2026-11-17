/**
 * js/estadisticas_juicios.js
 *
 * IMPORTANTE: no vi js/api.js ni js/toasts.js, así que este archivo es
 * autónomo. Si api.js ya expone una constante de base URL o un helper
 * de fetch, reemplaza `API_URL` / `llamar()` por los tuyos.
 */
(() => {
  const API_URL =
    typeof API_BASE !== "undefined" ? API_BASE : window.API_BASE || "";
  const ENDPOINT = `${API_URL}/api/estadisticas-juicios`;

  const SUPUESTOS = {
    conservador: { con_sync: 0.5, solo_svp: 0.25, sin_asignacion: 0.05, calif_oportuna: 0.5 },
    base: { con_sync: 0.7, solo_svp: 0.4, sin_asignacion: 0.1, calif_oportuna: 0.7 },
    optimista: { con_sync: 0.85, solo_svp: 0.6, sin_asignacion: 0.25, calif_oportuna: 0.9 },
  };
  const ETIQ = {
    con_sync: "Juicios ya vistos en Synchronize",
    solo_svp: "Juicios solo en SVP",
    sin_asignacion: "Juicios sin instructor",
    calif_oportuna: "Juicios nuevos calificados a tiempo",
  };

  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Number(n).toLocaleString("es-CO");
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function aviso(texto, tipo = "info") {
    if (typeof Toastify === "function") {
      Toastify({
        text: texto,
        duration: 4000,
        gravity: "top",
        position: "right",
        style: { background: tipo === "error" ? "#c0392b" : "#39a900" },
      }).showToast();
    } else {
      alert(texto);
    }
  }

  // ---------- supuestos editables
  function pintarSupuestos() {
    const claves = Object.keys(ETIQ);
    let html = "<tr><th>Escenario</th>" + claves.map((k) => `<th>${ETIQ[k]}</th>`).join("") + "</tr>";
    for (const esc_ of Object.keys(SUPUESTOS)) {
      html +=
        `<tr><td style="text-transform:capitalize">${esc_}</td>` +
        claves
          .map(
            (k) =>
              `<td><input type="number" step="0.05" min="0" max="1" data-esc="${esc_}" data-k="${k}" value="${SUPUESTOS[esc_][k]}"></td>`
          )
          .join("") +
        "</tr>";
    }
    $("tablaSupuestos").innerHTML = html;
  }

  function leerSupuestos() {
    const out = {};
    document.querySelectorAll("#tablaSupuestos input").forEach((i) => {
      const v = parseFloat(i.value);
      if (Number.isNaN(v)) return;
      (out[i.dataset.esc] ||= {})[i.dataset.k] = v;
    });
    return out;
  }

  // ---------- render
  const tabla = (cols, filas) =>
    `<div class="tabla-wrap"><table class="tabla"><thead><tr>${cols
      .map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.t)}</th>`)
      .join("")}</tr></thead><tbody>${filas
      .map((f) => `<tr>${cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.r ? c.r(f) : esc(f[c.k])}</td>`).join("")}</tr>`)
      .join("")}</tbody></table></div>`;

  const barra = (pct) => `<div class="barra"><i style="width:${Math.min(100, pct)}%"></i></div>`;

  function distribucion(titulo, datos) {
    return `<div class="bloque"><h3>${esc(titulo)}</h3>${tabla(
      [
        { t: "Etiqueta", k: "etiqueta" },
        { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) },
        { t: "%", num: true, r: (f) => f.pct + "%" },
        { t: "", r: (f) => barra(f.pct) },
      ],
      datos
    )}</div>`;
  }

  function renderProyeccion(p) {
    const e = p.estructural;
    const tarjetas = Object.entries(e.escenarios)
      .map(
        ([nombre, s]) => `
        <div class="esc">
          <h4>${esc(nombre)}</h4>
          <div class="gran">${fmt(s.pendientes_proyectados)}</div>
          <div>juicios pendientes al final del trimestre</div>
          <div>${s.reduccion_neta_pct >= 0 ? "▼" : "▲"} ${Math.abs(s.reduccion_neta_pct)}% ${s.reduccion_neta_pct >= 0 ? "menos" : "más"} que hoy</div>
          <ul>
            <li>Juicios que se califican: ${fmt(s.resueltos)}</li>
            <li>Juicios nuevos que quedan sin calificar: ${fmt(s.nuevos)}</li>
            <li>Juicios pendientes por aprendiz: ${s.promedio_por_aprendiz}</li>
          </ul>
        </div>`
      )
      .join("");

    const h = p.historica;
    const hist = h.disponible
      ? `<p><b>${fmt(h.pendientes_proyectados)}</b> juicios pendientes proyectados al ${esc(h.fecha_proyeccion)}
         (${h.cambio_por_dia > 0 ? "+" : ""}${h.cambio_por_dia} juicios por día; ${h.puntos} reportes; confiabilidad ${esc(h.confiabilidad)}).</p>`
      : `<p class="aviso">${esc(h.mensaje)}</p>`;

    return `
      <div class="bloque">
        <h3>¿Cuántos juicios pendientes habría en ${esc(p.trimestre_proyectado)}? (hoy: ${esc(p.trimestre_actual)})</h3>
        <p class="aviso">Hoy hay ${fmt(e.base_pendientes)} juicios pendientes. Cada trimestre cada aprendiz
          ve ${e.raps_por_trimestre} resultados de aprendizaje (RAP) y cada RAP genera un juicio:
          ${fmt(e.aprendices)} aprendices generan ${fmt(e.juicios_generados_trimestre)} juicios nuevos.</p>
        <div class="escenarios">${tarjetas}</div>
        <p class="aviso">${esc(e.advertencia)}</p>
      </div>
      <div class="bloque"><h3>Tendencia histórica</h3>${hist}</div>`;
  }

  function renderResultado(data) {
    const s = data.estadisticas;
    const r = s.resumen;
    const kpis = [
      [fmt(r.total_pendientes), "Total de juicios pendientes"],
      [fmt(r.aprendices), "Aprendices con juicios pendientes"],
      [fmt(r.fichas), "Fichas"],
      [fmt(r.programas), "Programas"],
      [r.promedio_por_aprendiz, "Promedio de juicios pendientes por aprendiz"],
      [fmt(s.riesgo_cierre.pendientes), `Juicios pendientes en fichas de ${esc(s.riesgo_cierre.trimestre || "—")} (por cerrar etapa)`],
    ]
      .map(([v, l]) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`)
      .join("");

    const ob = s.raps_por_trimestre_observado || {};
    const observado = ob.promedio
      ? ` · Observado en el archivo: ${ob.promedio} RAPs por ficha y trimestre`
      : "";
    const descarga = data.archivo_excel
      ? `<p><a class="btn-primary" style="display:inline-block;text-decoration:none"
            href="${ENDPOINT}/descargar/${encodeURIComponent(data.id_ejecucion)}/${encodeURIComponent(data.archivo_excel)}">
            Descargar Excel</a></p>`
      : "";

    $("resultado").innerHTML = `
      ${descarga}
      <p class="aviso">Reporte del ${esc(r.fecha_reporte)} · ${esc(r.archivo_origen)}${esc(observado)}</p>
      <div class="kpis">${kpis}</div>

      ${renderProyeccion(data.proyeccion)}

      <div class="bloque"><h3>Estado de los juicios pendientes</h3>${tabla(
        [
          { t: "Tipo", k: "descripcion" },
          { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) },
          { t: "%", num: true, r: (f) => f.pct + "%" },
          { t: "", r: (f) => barra(f.pct) },
        ],
        s.gestion
      )}</div>

      <div class="bloque"><h3>Juicios pendientes por programa de formación</h3>${tabla(
        [
          { t: "Programa", k: "programa" },
          { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) },
          { t: "%", num: true, r: (f) => f.pct + "%" },
          { t: "Aprendices", num: true, r: (f) => fmt(f.aprendices) },
          { t: "Fichas", num: true, r: (f) => fmt(f.fichas) },
          { t: "Juicios por aprendiz", num: true, k: "promedio_por_aprendiz" },
          { t: "Sin instructor", num: true, r: (f) => fmt(f.sin_asignacion) },
        ],
        s.por_programa
      )}</div>

      ${distribucion("Juicios pendientes por trimestre actual de la ficha", s.por_trimestre_ficha)}
      ${distribucion("Juicios pendientes según el trimestre en que vieron el resultado", s.por_trimestre_visto)}
      ${distribucion("Juicios pendientes según el trimestre del año programado en Synchronize", s.por_trimestre_programacion_sync)}

      <div class="bloque"><h3>Instructores con más juicios pendientes (programados en SVP)</h3>${tabla(
        [{ t: "Instructor", k: "nombre" }, { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_instructores_svp
      )}</div>
      <div class="bloque"><h3>Instructores con más juicios sin calificar (Synchronize)</h3>${tabla(
        [{ t: "Instructor", k: "nombre" }, { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_instructores_sync
      )}</div>
      <div class="bloque"><h3>Fichas con más juicios pendientes</h3>${tabla(
        [{ t: "Ficha", k: "ficha" }, { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_fichas
      )}</div>
      <div class="bloque"><h3>Aprendices con más juicios pendientes</h3>${tabla(
        [{ t: "Documento", k: "documento" }, { t: "Nombre", k: "nombre" }, { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_aprendices
      )}</div>
      <div class="bloque"><h3>Resultados de aprendizaje con más juicios pendientes</h3>${tabla(
        [{ t: "ID RAP", k: "id_rap" }, { t: "Resultado", k: "resultado" }, { t: "Juicios pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_raps
      )}</div>`;
  }

  // ---------- historial
  async function cargarHistorial() {
    try {
      const resp = await fetch(`${ENDPOINT}/historial?limite=10`);
      if (!resp.ok) throw new Error();
      const items = await resp.json();
      if (!items.length) {
        $("historial").innerHTML = '<p class="placeholder">Sin ejecuciones previas.</p>';
        return;
      }
      $("historial").innerHTML = items
        .map((it) => {
          let r = it.resultado;
          if (typeof r === "string") { try { r = JSON.parse(r); } catch { r = null; } }
          const res = r?.estadisticas?.resumen;
          const txt = res
            ? `${esc(res.fecha_reporte)} · ${fmt(res.total_pendientes)} juicios pendientes · ${fmt(res.aprendices)} aprendices`
            : "Ejecución";
          return `<div class="historial-item">${txt}</div>`;
        })
        .join("");
    } catch {
      $("historial").innerHTML = '<p class="placeholder">No se pudo cargar el historial.</p>';
    }
  }

  // ---------- eventos
  $("archivo").addEventListener("change", (e) => {
    const f = e.target.files[0];
    $("nombreArchivo").textContent = f ? f.name : "";
  });

  $("btnProcesar").addEventListener("click", async () => {
    const f = $("archivo").files[0];
    if (!f) return aviso("Selecciona un archivo .xlsx", "error");

    const fd = new FormData();
    fd.append("archivo", f);
    fd.append("raps_por_trimestre", $("raps").value || "10");
    fd.append("supuestos", JSON.stringify(leerSupuestos()));

    $("btnProcesar").disabled = true;
    $("progreso").classList.remove("oculto");
    try {
      const resp = await fetch(ENDPOINT, { method: "POST", body: fd });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || "Error al procesar el archivo");
      renderResultado(data);
      aviso("Análisis completado");
      cargarHistorial();
    } catch (err) {
      aviso(err.message, "error");
    } finally {
      $("btnProcesar").disabled = false;
      $("progreso").classList.add("oculto");
    }
  });

  pintarSupuestos();
  cargarHistorial();
})();