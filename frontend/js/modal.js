// modal.js — confirmación con estética del tema (reemplaza a confirm() nativo)

/**
 * Muestra un modal de confirmación y devuelve una Promesa<boolean>.
 *
 *   if (await confirmar({ titulo: "¿Borrar?", mensaje: "..." })) { ... }
 */
function confirmar({
  titulo = "¿Continuar?",
  mensaje = "",
  textoConfirmar = "Confirmar",
  textoCancelar = "Cancelar",
  peligro = false,
} = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <h3></h3>
        <p></p>
        <div class="modal-actions">
          <button class="modal-btn-cancel" type="button"></button>
          <button class="modal-btn-confirm${peligro ? " danger" : ""}" type="button"></button>
        </div>
      </div>
    `;

    // Se escribe con textContent para evitar inyección desde strings
    overlay.querySelector("h3").textContent = titulo;
    overlay.querySelector("p").textContent = mensaje;
    overlay.querySelector(".modal-btn-cancel").textContent = textoCancelar;
    overlay.querySelector(".modal-btn-confirm").textContent = textoConfirmar;

    document.body.appendChild(overlay);

    const confirmBtn = overlay.querySelector(".modal-btn-confirm");
    const cancelBtn = overlay.querySelector(".modal-btn-cancel");

    function cerrar(valor) {
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      resolve(valor);
    }

    function onKey(e) {
      if (e.key === "Escape") cerrar(false);
      if (e.key === "Enter") cerrar(true);
    }

    confirmBtn.addEventListener("click", () => cerrar(true));
    cancelBtn.addEventListener("click", () => cerrar(false));
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) cerrar(false);
    });
    document.addEventListener("keydown", onKey);

    confirmBtn.focus();
  });
}