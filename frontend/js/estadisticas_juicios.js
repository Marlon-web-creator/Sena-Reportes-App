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
    con_sync: "Visto en Synchronize",
    solo_svp: "Solo SVP",
    sin_asignacion: "Sin asignación",
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
        { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) },
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
          <div>${s.reduccion_neta_pct >= 0 ? "▼" : "▲"} ${Math.abs(s.reduccion_neta_pct)}% neto vs hoy</div>
          <ul>
            <li>Resueltos: ${fmt(s.resueltos)}</li>
            <li>Nuevos que quedan pendientes: ${fmt(s.nuevos)}</li>
            <li>Promedio por aprendiz: ${s.promedio_por_aprendiz}</li>
          </ul>
        </div>`
      )
      .join("");

    const h = p.historica;
    const hist = h.disponible
      ? `<p><b>${fmt(h.pendientes_proyectados)}</b> pendientes proyectados al ${esc(h.fecha_proyeccion)}
         (${h.cambio_por_dia > 0 ? "+" : ""}${h.cambio_por_dia}/día; ${h.puntos} reportes; confiabilidad ${esc(h.confiabilidad)}).</p>`
      : `<p class="aviso">${esc(h.mensaje)}</p>`;

    return `
      <div class="bloque">
        <h3>Proyección para ${esc(p.trimestre_proyectado)} (hoy: ${esc(p.trimestre_actual)})</h3>
        <p class="aviso">Base actual: ${fmt(e.base_pendientes)} pendientes. Cada trimestre se ven
          ${e.raps_por_trimestre} RAPs por aprendiz: ${fmt(e.aprendices)} aprendices generan
          ${fmt(e.juicios_generados_trimestre)} juicios nuevos.</p>
        <div class="escenarios">${tarjetas}</div>
        <p class="aviso">${esc(e.advertencia)}</p>
      </div>
      <div class="bloque"><h3>Tendencia histórica</h3>${hist}</div>`;
  }

  function renderResultado(data) {
    const s = data.estadisticas;
    const r = s.resumen;
    const kpis = [
      [fmt(r.total_pendientes), "Pendientes"],
      [fmt(r.aprendices), "Aprendices"],
      [fmt(r.fichas), "Fichas"],
      [fmt(r.programas), "Programas"],
      [r.promedio_por_aprendiz, "Promedio por aprendiz"],
      [fmt(s.riesgo_cierre.pendientes), `En ${esc(s.riesgo_cierre.trimestre || "—")} (riesgo cierre)`],
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

      <div class="bloque"><h3>Estado de gestión de los pendientes</h3>${tabla(
        [
          { t: "Tipo", k: "descripcion" },
          { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) },
          { t: "%", num: true, r: (f) => f.pct + "%" },
          { t: "", r: (f) => barra(f.pct) },
        ],
        s.gestion
      )}</div>

      <div class="bloque"><h3>Por programa de formación</h3>${tabla(
        [
          { t: "Programa", k: "programa" },
          { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) },
          { t: "%", num: true, r: (f) => f.pct + "%" },
          { t: "Aprendices", num: true, r: (f) => fmt(f.aprendices) },
          { t: "Fichas", num: true, r: (f) => fmt(f.fichas) },
          { t: "Prom./aprendiz", num: true, k: "promedio_por_aprendiz" },
          { t: "Sin asignación", num: true, r: (f) => fmt(f.sin_asignacion) },
        ],
        s.por_programa
      )}</div>

      ${distribucion("Por trimestre actual de la ficha", s.por_trimestre_ficha)}
      ${distribucion("Trimestre en que vieron el resultado", s.por_trimestre_visto)}
      ${distribucion("Trimestre del año programado en Synchronize", s.por_trimestre_programacion_sync)}

      <div class="bloque"><h3>Top instructores con pendientes (SVP)</h3>${tabla(
        [{ t: "Instructor", k: "nombre" }, { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_instructores_svp
      )}</div>
      <div class="bloque"><h3>Top instructores sin calificar (Synchronize)</h3>${tabla(
        [{ t: "Instructor", k: "nombre" }, { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_instructores_sync
      )}</div>
      <div class="bloque"><h3>Top fichas</h3>${tabla(
        [{ t: "Ficha", k: "ficha" }, { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_fichas
      )}</div>
      <div class="bloque"><h3>Aprendices con más pendientes</h3>${tabla(
        [{ t: "Documento", k: "documento" }, { t: "Nombre", k: "nombre" }, { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) }],
        s.top_aprendices
      )}</div>
      <div class="bloque"><h3>Resultados de aprendizaje con más pendientes</h3>${tabla(
        [{ t: "ID RAP", k: "id_rap" }, { t: "Resultado", k: "resultado" }, { t: "Pendientes", num: true, r: (f) => fmt(f.pendientes) }],
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
            ? `${esc(res.fecha_reporte)} · ${fmt(res.total_pendientes)} pendientes · ${fmt(res.aprendices)} aprendices`
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