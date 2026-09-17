const API = "/api/archivos";

let pestanaActiva = "subidos";

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


/* ============================================================
   ARCHIVOS SUBIDOS
   ============================================================ */

async function cargarSubidos() {
  try {
    const res = await fetch(`${API}/subidos`);
    const data = await leerRespuesta(res);

    const tbody = document.getElementById("tabla-subidos");

    tbody.innerHTML = data.map((f) => `
      <tr>
        <td>${f.nombre_original}</td>
        <td>${f.modulo || "-"}</td>
        <td>${formatBytes(f.tamano_bytes)}</td>
        <td>${f.fecha_subida}</td>
        <td>
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
      ? "archivos subidos"
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
       * Archivos subidos:
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
        await cargarSubidos();
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

    const archivo =
      document.getElementById("input-archivo").files[0];

    const modulo =
      document.getElementById("input-modulo").value;


    if (!archivo) {
      notificar(
        "Selecciona un archivo.",
        "error"
      );

      return;
    }


    const fd = new FormData();

    fd.append(
      "archivo",
      archivo
    );

    if (modulo) {
      fd.append(
        "modulo",
        modulo
      );
    }


    try {

      const res = await fetch(
        `${API}/subidos`,
        {
          method: "POST",
          body: fd,
        }
      );


      await leerRespuesta(res);


      notificar(
        "Archivo subido correctamente",
        "success"
      );


      e.target.reset();

      await cargarSubidos();


    } catch (err) {

      console.error(
        "Error subiendo archivo:",
        err
      );

      notificar(
        `Error al subir el archivo: ${err.message}`,
        "error"
      );
    }
  });


/* ============================================================
   INICIALIZACIÓN
   ============================================================ */

cargarSubidos();
cargarGenerados();
