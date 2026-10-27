/* Módulo "Actas y Resoluciones" — frontend.
 * POST /api/actas-resoluciones (campo "consolidado") -> {id_ejecucion}
 * GET  /api/actas-resoluciones/progreso/{id}           (se consulta cada 1.5 s)
 * GET  /api/actas-resoluciones/descargar/{id}/{archivo}
 */
(() => {
  const API = "/api/actas-resoluciones";
  const $ = (id) => document.getElementById(id);
  const el = {
    drop: $("ar-drop"), input: $("ar-input"), file: $("ar-file"), run: $("ar-run"),
    form: $("ar-form-card"), prog: $("ar-progress-card"), err: $("ar-error-card"), res: $("ar-result-card"),
    bar: $("ar-bar"), fill: $("ar-bar-fill"), msg: $("ar-msg"), errTxt: $("ar-error"),
    stats: $("ar-stats"), warns: $("ar-warns"), dl: $("ar-download"),
  };
  let archivo = null;
  let timer = null;

  const toast = (t, bg = "#1f4e2c") =>
    window.Toastify && Toastify({ text: t, duration: 4000, gravity: "top", position: "right", style: { background: bg } }).showToast();
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const show = (card) => {
    [el.form, el.prog, el.err, el.res].forEach((c) => c.classList.add("ar-hide"));
    card.classList.remove("ar-hide");
  };

  // ---------- selección de archivo
  function elegir(f) {
    if (!f) return;
    if (!/\.(xlsx|xlsm)$/i.test(f.name)) { toast("El consolidado debe ser .xlsx o .xlsm", "#c0392b"); return; }
    archivo = f;
    el.file.textContent = f.name;
    el.run.disabled = false;
  }
  el.drop.addEventListener("click", () => el.input.click());
  el.drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.input.click(); } });
  el.input.addEventListener("change", () => elegir(el.input.files[0]));
  ["dragenter", "dragover"].forEach((ev) => el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.add("ar-over"); }));
  ["dragleave", "drop"].forEach((ev) => el.drop.addEventListener(ev, (e) => { e.preventDefault(); el.drop.classList.remove("ar-over"); }));
  el.drop.addEventListener("drop", (e) => elegir(e.dataTransfer.files[0]));

  // ---------- ejecución
  el.run.addEventListener("click", async () => {
    if (!archivo) return;
    el.run.disabled = true;
    const fd = new FormData();
    fd.append("consolidado", archivo);
    try {
      const r = await fetch(API, { method: "POST", body: fd });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `Error ${r.status}`);
      empezarProgreso(data.id_ejecucion);
    } catch (e) {
      mostrarError(e.message);
    }
  });

  function empezarProgreso(id) {
    show(el.prog);
    el.bar.classList.add("ar-ind");
    el.fill.style.width = "";
    el.msg.textContent = "Descargando PDFs y analizando…";
    clearInterval(timer);
    const tick = async () => {
      try {
        const r = await fetch(`${API}/progreso/${encodeURIComponent(id)}`);
        if (!r.ok) throw new Error(`No se pudo consultar el progreso (${r.status})`);
        const s = normalizar(await r.json());
        if (s.mensaje) el.msg.textContent = s.mensaje;
        if (s.total > 0) {
          el.bar.classList.remove("ar-ind");
          el.fill.style.width = Math.min(100, Math.round((s.actual / s.total) * 100)) + "%";
        }
        if (s.error) { clearInterval(timer); mostrarError(s.error); }
        else if (s.terminado) { clearInterval(timer); mostrarResultado(id, s.resultado || {}); }
      } catch (e) { clearInterval(timer); mostrarError(e.message); }
    };
    timer = setInterval(tick, 1500);
    tick();
  }

  // Tolerante a los nombres de campo que devuelva progreso.consultar().
  function normalizar(d) {
    const estado = String(d.estado ?? d.status ?? "").toLowerCase();
    const error = d.error || (/error|fall/.test(estado) ? (d.mensaje || d.detalle || "Error desconocido") : null);
    const terminado = !error && (d.resultado != null || /ok|final|complet|termin|done|listo/.test(estado));
    return {
      mensaje: d.mensaje ?? d.message ?? "",
      actual: Number(d.actual ?? d.current ?? 0),
      total: Number(d.total ?? 0),
      resultado: d.resultado ?? d.result ?? null,
      error, terminado,
    };
  }

  // ---------- UI de salida
  function mostrarError(msg) { el.errTxt.textContent = msg; show(el.err); }

  function stat(valor, texto, cls = "") {
    return `<div class="ar-stat ${cls}"><b>${esc(valor ?? 0)}</b><span>${esc(texto)}</span></div>`;
  }

  function mostrarResultado(id, r) {
    el.stats.innerHTML =
      stat(r.total_aprendices, "Aprendices en el consolidado") +
      stat(r.total_en_resolucion_cancelacion, "En resolución de cancelación", "ar-r") +
      stat(r.total_condicionados, "Condicionados", "ar-a") +
      stat(r.total_por_revisar, "Por revisar manualmente", "ar-b") +
      stat(r.total_con_cambio_de_estado, "Estado real distinto al del consolidado") +
      stat(r.total_sin_novedad, "Sin novedad en los PDFs") +
      stat((r.pdfs_usados || []).length, "PDFs revisados") +
      stat(r.paginas_total, "Páginas revisadas");

    const avisos = [];
    const lista = (titulo, items) => items.length ? `<div class="ar-warn"><b>${esc(titulo)}</b><ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : "";
    avisos.push(lista("PDFs omitidos", (r.pdfs_omitidos || []).map((o) => `${o.archivo}: ${o.motivo}`)));
    avisos.push(lista("PDFs sin texto (¿escaneados?, no se pudieron revisar)", r.pdfs_sin_texto || []));
    avisos.push(lista("PDFs que no se pudieron descargar", r.pdfs_fallidos_al_descargar || []));
    avisos.push(lista("Hojas del consolidado omitidas", (r.hojas_omitidas || []).map((o) => `${o.hoja}: ${o.motivo}`)));
    if (r.paginas_ocr) avisos.push(`<div class="ar-warn">${esc(r.paginas_ocr)} página(s) se leyeron con OCR: conviene verificarlas en la hoja "Evidencias PDF".</div>`);
    if (r.paginas_sin_texto && !(r.pdfs_sin_texto || []).length) avisos.push(`<div class="ar-warn">${esc(r.paginas_sin_texto)} página(s) sin texto no se revisaron.</div>`);
    if (r.archivos_no_pdf_ignorados) avisos.push(`<div class="ar-warn">${esc(r.archivos_no_pdf_ignorados)} archivo(s) del módulo no son PDF y se ignoraron.</div>`);
    el.warns.innerHTML = avisos.join("");

    const nombre = String(r.archivo_generado || "").split("/").pop();
    el.dl.href = `${API}/descargar/${encodeURIComponent(id)}/${encodeURIComponent(nombre)}`;
    el.dl.classList.toggle("ar-hide", !nombre);
    show(el.res);
    toast("Listo: el Excel está generado");
  }

  function reiniciar() {
    clearInterval(timer);
    archivo = null; el.input.value = ""; el.file.textContent = ""; el.run.disabled = true;
    show(el.form);
  }
  $("ar-again").addEventListener("click", reiniciar);
  $("ar-retry").addEventListener("click", reiniciar);
})();