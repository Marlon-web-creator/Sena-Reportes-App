// modal.js — confirmación y entrada de texto con estética del tema
// (reemplaza a confirm() y prompt() nativos)

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


/**
 * Muestra un modal con un campo de texto y devuelve una Promesa<string|null>.
 * Devuelve null si el usuario cancela o deja el campo vacío.
 *
 *   const nombre = await pedirTexto({ titulo: "Nueva carpeta", mensaje: "Nombre:" });
 *   if (nombre) { ... }
 */
function pedirTexto({
  titulo = "Escribe un valor",
  mensaje = "",
  placeholder = "",
  valorInicial = "",
  textoConfirmar = "Aceptar",
  textoCancelar = "Cancelar",
} = {}) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <h3></h3>
        <p></p>
        <input type="text" class="modal-input" />
        <div class="modal-actions">
          <button class="modal-btn-cancel" type="button"></button>
          <button class="modal-btn-confirm" type="button"></button>
        </div>
      </div>
    `;

    overlay.querySelector("h3").textContent = titulo;
    overlay.querySelector("p").textContent = mensaje;

    const input = overlay.querySelector(".modal-input");
    input.placeholder = placeholder;
    input.value = valorInicial;

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

    function valorLimpio() {
      const texto = input.value.trim();
      return texto ? texto : null;
    }

    function onKey(e) {
      if (e.key === "Escape") cerrar(null);
      if (e.key === "Enter") {
        e.preventDefault();
        cerrar(valorLimpio());
      }
    }

    confirmBtn.addEventListener("click", () => cerrar(valorLimpio()));
    cancelBtn.addEventListener("click", () => cerrar(null));
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) cerrar(null);
    });
    document.addEventListener("keydown", onKey);

    input.focus();
  });
}