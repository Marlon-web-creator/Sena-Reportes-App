const API = "/api/archivos";

let pestanaActiva = "subidos";

// Estado de navegación por carpetas.
// carpetaActualId === null  →  estamos en la raíz.
let carpetaActualId = null;
let rutaActual = [{ id: null, nombre: "Raíz" }];

/* ============================================================
   PESTAÑAS
   ============================================================ */

document.querySelectorAll(".db-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".db-tab").forEach((b) => {
      b.classList.remove("active");
    });

    document.querySelectorAll(".db-panel").forEach((p) => {
      p.classList.remove("active");
    });

    btn.classList.add("active");

    pestanaActiva = btn.dataset.tab;

    document
      .getElementById(`tab-${pestanaActiva}`)
      .classList.add("active");
  });
});


/* ============================================================
   UTILIDADES
   ============================================================ */

function formatBytes(bytes) {
  if (!bytes) return "-";

  const kb = bytes / 1024;

  return kb > 1024
    ? `${(kb / 1024).toFixed(1)} MB`
    : `${kb.toFixed(1)} KB`;
}


async function leerRespuesta(res) {
  const data = await res.json().catch(() => null);

  if (!res.ok) {
    const detalle =
      data?.detail ||
      `Error HTTP ${res.status} ${res.statusText}`;

    throw new Error(detalle);
  }

  return data;
}


function iconoCarpeta() {
  return `
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none"
         stroke="currentColor" stroke-width="1.8"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" />
    </svg>
  `;
}


function iconoNuevaCarpeta() {
  return `
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none"
         stroke="currentColor" stroke-width="1.9"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" />
      <path d="M12 11v4" />
      <path d="M10 13h4" />
    </svg>
  `;
}


/* ============================================================
   CARPETAS
   ============================================================ */

async function cargarCarpetas() {
  try {
    const url =
      carpetaActualId === null
        ? `${API}/carpetas`
        : `${API}/carpetas?padre_id=${carpetaActualId}`;

    const res = await fetch(url);
    const data = await leerRespuesta(res);

    renderCarpetas(data);
  } catch (err) {
    console.error("Error cargando carpetas:", err);

    notificar(
      `No se pudieron cargar las carpetas: ${err.message}`,
      "error"
    );
  }
}


function renderCarpetas(carpetas) {
  const grid = document.getElementById("grid-carpetas");

  if (!grid) return;

  if (!carpetas.length) {
    grid.innerHTML = "";
    return;
  }

  grid.innerHTML = carpetas.map((c) => `
    <div class="carpeta-card" data-id="${c.id}">
      <div class="carpeta-abrir" data-id="${c.id}" data-nombre="${c.nombre}">
        ${iconoCarpeta()}
        <span>${c.nombre}</span>
      </div>
      <button
        type="button"
        class="btn-eliminar-carpeta"
        data-id="${c.id}"
        data-nombre="${c.nombre}"
        title="Eliminar carpeta"
      >
        &times;
      </button>
    </div>
  `).join("");

  grid.querySelectorAll(".carpeta-abrir").forEach((el) => {
    el.addEventListener("click", () => {
      abrirCarpeta(Number(el.dataset.id), el.dataset.nombre);
    });
  });

  grid.querySelectorAll(".btn-eliminar-carpeta").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await eliminarCarpeta(Number(btn.dataset.id), btn.dataset.nombre);
    });
  });
}


function abrirCarpeta(id, nombre) {
  carpetaActualId = id;
  rutaActual.push({ id, nombre });

  renderBreadcrumb();
  cargarTodo();
}


function renderBreadcrumb() {
  const cont = document.getElementById("breadcrumb-carpetas");

  if (!cont) return;

  cont.innerHTML = rutaActual.map((c, i) => {
    const esUltimo = i === rutaActual.length - 1;

    return `
      <span class="breadcrumb-item${esUltimo ? " activo" : ""}" data-index="${i}">
        ${c.nombre}
      </span>
      ${esUltimo ? "" : '<span class="breadcrumb-sep">/</span>'}
    `;
  }).join("");

  cont.querySelectorAll(".breadcrumb-item").forEach((el) => {
    el.addEventListener("click", () => {
      const i = Number(el.dataset.index);

      if (i === rutaActual.length - 1) return;

      rutaActual = rutaActual.slice(0, i + 1);
      carpetaActualId = rutaActual[rutaActual.length - 1].id;

      renderBreadcrumb();
      cargarTodo();
    });
  });
}


async function crearCarpeta() {
  const nombre = await pedirTexto({
    titulo: "Nueva carpeta",
    mensaje: "Nombre de la carpeta:",
    placeholder: "Ej. Fichas 2026",
  });

  if (!nombre) return;

  try {
    const res = await fetch(`${API}/carpetas`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        nombre,
        padre_id: carpetaActualId,
      }),
    });

    await leerRespuesta(res);

    notificar("Carpeta creada", "success");

    await cargarCarpetas();
  } catch (err) {
    console.error("Error creando carpeta:", err);

    notificar(
      `No se pudo crear la carpeta: ${err.message}`,
      "error"
    );
  }
}


async function eliminarCarpeta(id, nombre) {
  let mensaje = `Se eliminará la carpeta "${nombre}"`;

  try {
    const res = await fetch(`${API}/carpetas/${id}/contenido`);
    const info = await leerRespuesta(res);

    if (info.subcarpetas || info.archivos) {
      mensaje +=
        `, junto con ${info.subcarpetas} subcarpeta(s) y ` +
        `${info.archivos} archivo(s) que contiene`;
    }
  } catch (err) {
    console.warn("No se pudo obtener el contenido de la carpeta:", err);
  }

  mensaje += ". Esta acción no se puede deshacer.";

  const ok = await confirmar({
    titulo: "¿Eliminar carpeta?",
    mensaje,
    textoConfirmar: "Eliminar",
    peligro: true,
  });

  if (!ok) return;

  try {
    const res = await fetch(`${API}/carpetas/${id}`, {
      method: "DELETE",
    });

    await leerRespuesta(res);

    notificar("Carpeta eliminada", "success");

    await cargarTodo();
  } catch (err) {
    console.error("Error eliminando carpeta:", err);

    notificar(
      `No se pudo eliminar la carpeta: ${err.message}`,
      "error"
    );
  }
}


/**
 * Construye las opciones para el selector "mover a..." de cada archivo,
 * a partir del árbol completo de carpetas, con sangría según profundidad.
 */
async function obtenerOpcionesCarpetas() {
  const res = await fetch(`${API}/carpetas/arbol`);
  const carpetas = await leerRespuesta(res);

  const porPadre = {};

  carpetas.forEach((c) => {
    const clave = c.padre_id ?? "raiz";
    if (!porPadre[clave]) porPadre[clave] = [];
    porPadre[clave].push(c);
  });

  const opciones = [{ id: "", etiqueta: "Raíz" }];

  function recorrer(clavePadre, profundidad) {
    (porPadre[clavePadre] || [])
      .slice()
      .sort((a, b) => a.nombre.localeCompare(b.nombre))
      .forEach((c) => {
        opciones.push({
          id: c.id,
          etiqueta: `${"—".repeat(profundidad)} ${c.nombre}`.trim(),
        });

        recorrer(c.id, profundidad + 1);
      });
  }

  recorrer("raiz", 1);

  return opciones;
}


/* ============================================================
   ARCHIVOS SUBIDOS
   ============================================================ */

async function cargarSubidos() {
  try {
    const url =
      carpetaActualId === null
        ? `${API}/subidos`
        : `${API}/subidos?carpeta_id=${carpetaActualId}`;

    const res = await fetch(url);
    const data = await leerRespuesta(res);

    const opciones = await obtenerOpcionesCarpetas();

    const tbody = document.getElementById("tabla-subidos");

    tbody.innerHTML = data.map((f) => `
      <tr>
        <td>${f.nombre_original}</td>
        <td>${f.modulo || "-"}</td>
        <td>${formatBytes(f.tamano_bytes)}</td>
        <td>${f.fecha_subida}</td>
        <td>
          <select class="select-mover-archivo" data-id="${f.id}" title="Mover a...">
            ${opciones.map((o) => `
              <option
                value="${o.id}"
                ${String(o.id) === String(f.carpeta_id ?? "") ? "selected" : ""}
              >
                ${o.etiqueta}
              </option>
            `).join("")}
          </select>

          <a href="${API}/subidos/${f.id}/descargar">
            Descargar
          </a>

          <button
            type="button"
            data-id="${f.id}"
            class="btn-eliminar-subido"
          >
            Eliminar
          </button>
        </td>
      </tr>
    `).join("");

    tbody.querySelectorAll(".select-mover-archivo").forEach((sel) => {
      sel.addEventListener("change", () => {
        moverArchivo(Number(sel.dataset.id), sel.value);
      });
    });

    tbody.querySelectorAll(".btn-eliminar-subido").forEach((btn) => {
      btn.addEventListener("click", async () => {

        const ok = await confirmar({
          titulo: "¿Eliminar archivo?",
          mensaje:
            "Se borrará el registro y el archivo físico del disco. " +
            "Esta acción no se puede deshacer.",
          textoConfirmar: "Eliminar",
          peligro: true,
        });

        if (!ok) return;

        try {
          const res = await fetch(
            `${API}/subidos/${btn.dataset.id}`,
            {
              method: "DELETE",
            }
          );

          await leerRespuesta(res);

          notificar(
            "Archivo eliminado",
            "success"
          );

          await cargarSubidos();

        } catch (err) {
          console.error(
            "Error eliminando archivo:",
            err
          );

          notificar(
            `No se pudo eliminar el archivo: ${err.message}`,
            "error"
          );
        }
      });
    });

  } catch (err) {
    console.error(
      "Error cargando archivos subidos:",
      err
    );

    notificar(
      `No se pudieron cargar los archivos: ${err.message}`,
      "error"
    );
  }
}


async function moverArchivo(archivoId, carpetaIdValor) {
  const carpetaId = carpetaIdValor === "" ? null : Number(carpetaIdValor);

  try {
    const res = await fetch(`${API}/subidos/${archivoId}/mover`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ carpeta_id: carpetaId }),
    });

    await leerRespuesta(res);

    notificar("Archivo movido", "success");

    await cargarSubidos();
  } catch (err) {
    console.error("Error moviendo archivo:", err);

    notificar(
      `No se pudo mover el archivo: ${err.message}`,
      "error"
    );
  }
}


/* ============================================================
   ARCHIVOS GENERADOS
   ============================================================ */

async function cargarGenerados() {
  try {
    const res = await fetch(`${API}/generados`);
    const data = await leerRespuesta(res);

    const tbody = document.getElementById("tabla-generados");

    tbody.innerHTML = data.map((f) => `
      <tr>
        <td>${f.modulo}</td>
        <td>${f.nombre_archivo}</td>
        <td>${f.fecha}</td>
        <td>
          ${f.existe ? "Disponible" : "No encontrado"}
        </td>
        <td>

          ${
            f.existe
              ? `
                <a href="${API}/generados/${f.ejecucion_id}/${f.clave}/descargar">
                  Descargar
                </a>
              `
              : ""
          }

          <button
            type="button"
            data-ejecucion="${f.ejecucion_id}"
            data-clave="${f.clave}"
            class="btn-eliminar-generado"
          >
            Eliminar
          </button>

        </td>
      </tr>
    `).join("");

    tbody
      .querySelectorAll(".btn-eliminar-generado")
      .forEach((btn) => {

        btn.addEventListener("click", async () => {

          const ok = await confirmar({
            titulo: "¿Eliminar archivo generado?",
            mensaje:
              "Se borrará el archivo físico y se quitará " +
              "del registro del módulo.",
            textoConfirmar: "Eliminar",
            peligro: true,
          });

          if (!ok) return;

          try {
            const res = await fetch(
              `${API}/generados/${btn.dataset.ejecucion}/${btn.dataset.clave}`,
              {
                method: "DELETE",
              }
            );

            await leerRespuesta(res);

            notificar(
              "Archivo eliminado",
              "success"
            );

            await cargarGenerados();

          } catch (err) {
            console.error(
              "Error eliminando archivo generado:",
              err
            );

            notificar(
              `No se pudo eliminar el archivo: ${err.message}`,
              "error"
            );
          }
        });
      });

  } catch (err) {
    console.error(
      "Error cargando archivos generados:",
      err
    );

    notificar(
      `No se pudieron cargar los archivos generados: ${err.message}`,
      "error"
    );
  }
}


/* ============================================================
   ELIMINAR TODO
   ============================================================ */

document
  .getElementById("btn-eliminar-todo")
  .addEventListener("click", async () => {

    const esSubidos = pestanaActiva === "subidos";

    const etiqueta = esSubidos
      ? "archivos y carpetas subidos"
      : "archivos generados";


    /* --------------------------------------------------------
       Confirmación 1
       -------------------------------------------------------- */

    const primera = await confirmar({
      titulo: `¿Eliminar TODOS los ${etiqueta}?`,
      mensaje:
        "Se borrarán los registros y los archivos físicos " +
        "del disco. Esta acción no se puede deshacer.",
      textoConfirmar: "Continuar",
      peligro: true,
    });

    if (!primera) return;


    /* --------------------------------------------------------
       Confirmación 2
       -------------------------------------------------------- */

    const segunda = await confirmar({
      titulo: "Confirma una vez más",
      mensaje:
        `¿Seguro que quieres eliminar definitivamente ` +
        `todos los ${etiqueta}?`,
      textoConfirmar: "Sí, eliminar todo",
      peligro: true,
    });

    if (!segunda) return;


    const btn = document.getElementById(
      "btn-eliminar-todo"
    );

    btn.disabled = true;


    try {

      /*
       * IMPORTANTE:
       *
       * No usamos IDs aquí.
       *
       * Archivos subidos (y carpetas):
       *     DELETE /api/archivos/subidos
       *
       * Archivos generados:
       *     DELETE /api/archivos/generados
       */

      const url = esSubidos
        ? `${API}/subidos`
        : `${API}/generados`;


      console.log(
        ">>> Eliminación masiva:",
        url
      );


      const res = await fetch(url, {
        method: "DELETE",
      });


      const data = await leerRespuesta(res);


      console.log(
        ">>> Respuesta eliminación masiva:",
        data
      );


      notificar(
        `Eliminados: ${data.registros_eliminados} registro(s), ` +
        `${data.archivos_borrados} archivo(s) del disco`,
        "success"
      );


      if (esSubidos) {
        carpetaActualId = null;
        rutaActual = [{ id: null, nombre: "Raíz" }];
        renderBreadcrumb();

        await cargarTodo();
      } else {
        await cargarGenerados();
      }


    } catch (err) {

      console.error(
        ">>> ERROR EN ELIMINADO MASIVO:",
        err
      );

      notificar(
        `No se pudo eliminar todo: ${err.message}`,
        "error"
      );

    } finally {

      btn.disabled = false;

    }
  });


/* ============================================================
   SUBIDA DE ARCHIVOS
   ============================================================ */

document
  .getElementById("form-subir")
  .addEventListener("submit", async (e) => {

    e.preventDefault();

    const archivos = Array.from(
      document.getElementById("input-archivo").files
    );

    const modulo =
      document.getElementById("input-modulo").value;


    if (!archivos.length) {
      notificar(
        "Selecciona al menos un archivo.",
        "error"
      );

      return;
    }


    const fd = new FormData();

    // El backend espera la MISMA clave "archivos" repetida una vez
    // por cada archivo (así FastAPI la mapea a list[UploadFile]).
    for (const archivo of archivos) {
      fd.append("archivos", archivo);
    }

    if (modulo) {
      fd.append("modulo", modulo);
    }

    // El archivo se sube dentro de la carpeta donde estamos parados.
    if (carpetaActualId !== null) {
      fd.append("carpeta_id", carpetaActualId);
    }


    const btnSubmit = e.target.querySelector(
      'button[type="submit"]'
    );

    if (btnSubmit) btnSubmit.disabled = true;


    try {

      const res = await fetch(
        `${API}/subidos`,
        {
          method: "POST",
          body: fd,
        }
      );


      const data = await leerRespuesta(res);


      // data tiene la forma:
      // {
      //   total, total_subidos, total_fallidos,
      //   subidos: [...], fallidos: [...]
      // }

      if (data.total_fallidos > 0) {

        const nombresFallidos = (data.fallidos || [])
          .map((f) => f.nombre_original || "(sin nombre)")
          .join(", ");

        notificar(
          `Subidos: ${data.total_subidos}/${data.total}. ` +
          `Fallaron: ${nombresFallidos}`,
          data.total_subidos > 0 ? "success" : "error"
        );

      } else {

        notificar(
          data.total_subidos === 1
            ? "Archivo subido correctamente"
            : `${data.total_subidos} archivos subidos correctamente`,
          "success"
        );
      }


      e.target.reset();

      await cargarSubidos();


    } catch (err) {

      console.error(
        "Error subiendo archivos:",
        err
      );

      notificar(
        `Error al subir los archivos: ${err.message}`,
        "error"
      );

    } finally {

      if (btnSubmit) btnSubmit.disabled = false;

    }
  });

/* ============================================================
   BOTÓN "NUEVA CARPETA"
   ============================================================ */

const btnNuevaCarpeta = document.getElementById("btn-nueva-carpeta");

if (btnNuevaCarpeta) {
  btnNuevaCarpeta.addEventListener("click", crearCarpeta);
}


/* ============================================================
   INICIALIZACIÓN
   ============================================================ */

async function cargarTodo() {
  await Promise.all([
    cargarCarpetas(),
    cargarSubidos(),
  ]);
}

renderBreadcrumb();
cargarTodo();
cargarGenerados();