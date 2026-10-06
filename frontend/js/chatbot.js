/*
 * chatbot.js — Widget flotante del asistente de documentación.
 * Se monta solo: basta incluir este script (y chatbot.css) en la página.
 */
(function () {
  "use strict";

  if (window.__chatbotSena) return; // evita montarlo dos veces
  window.__chatbotSena = true;

  var URL_PREGUNTAR = "/api/chatbot/preguntar";
  var CLAVE_STORAGE = "chatbot_historial_v1";
  var MAX_GUARDADOS = 30;
  var MAX_ENVIADOS = 6;
  var BIENVENIDA =
    "Hola, soy el asistente de la documentación. Hazme una pregunta sobre los archivos cargados.";

  // Íconos vectoriales: mismo estilo que el index (trazo 1.8, extremos redondeados).
  var ICONOS = {
    chat:
      '<path d="M20 11.5a7.5 7.5 0 0 1-10.9 6.7L4 19.5l1.3-4.4A7.5 7.5 0 1 1 20 11.5z" />' +
      '<path d="M9 10.5h6M9 14h3.5" />',
    nuevo: '<path d="M20 11a8 8 0 1 0-2.3 5.7" /><path d="M20 4v7h-7" />',
    cerrar: '<path d="M6 6l12 12M18 6 6 18" />',
    enviar: '<path d="M21 3 10 14" /><path d="M21 3l-7 18-4-7-7-4 18-7z" />',
  };

  function icono(nombre) {
    return (
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      ICONOS[nombre] +
      "</svg>"
    );
  }

  // Si tu login usa un token en header (en vez de cookie de sesión),
  // devuélvelo aquí. Ej: return { Authorization: "Bearer " + localStorage.getItem("token") };
  function cabecerasAuth() {
    return {};
  }

  var mensajes = []; // { rol, contenido, fuentes?, error? }
  var ocupado = false;
  var raiz, boton, panel, listaEl, entradaEl, enviarEl;

  // ---------- utilidades ----------

  function escaparHtml(t) {
    return String(t)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatearRespuesta(t) {
    return escaparHtml(t)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\n/g, "<br>");
  }

  function guardar() {
    try {
      sessionStorage.setItem(CLAVE_STORAGE, JSON.stringify(mensajes.slice(-MAX_GUARDADOS)));
    } catch (e) {}
  }

  function cargar() {
    try {
      var crudo = sessionStorage.getItem(CLAVE_STORAGE);
      var datos = crudo ? JSON.parse(crudo) : [];
      return Array.isArray(datos) ? datos : [];
    } catch (e) {
      return [];
    }
  }

  function bajarScroll() {
    listaEl.scrollTop = listaEl.scrollHeight;
  }

  // ---------- render de mensajes ----------

  function crearBurbuja(m) {
    var burbuja = document.createElement("div");
    burbuja.className = "cb-msg " + (m.rol === "user" ? "cb-msg-user" : "cb-msg-bot");
    if (m.error) burbuja.className += " cb-msg-error";

    var texto = document.createElement("div");
    texto.className = "cb-msg-texto";
    if (m.rol === "user") {
      texto.textContent = m.contenido;
    } else {
      texto.innerHTML = formatearRespuesta(m.contenido);
    }
    burbuja.appendChild(texto);

    if (m.fuentes && m.fuentes.length) {
      var fuentes = document.createElement("div");
      fuentes.className = "cb-fuentes";
      fuentes.textContent = "Fuentes: " + m.fuentes.join(", ");
      burbuja.appendChild(fuentes);
    }
    return burbuja;
  }

  function pintarTodo() {
    listaEl.innerHTML = "";
    listaEl.appendChild(crearBurbuja({ rol: "assistant", contenido: BIENVENIDA }));
    mensajes.forEach(function (m) {
      listaEl.appendChild(crearBurbuja(m));
    });
    bajarScroll();
  }

  function agregar(m) {
    mensajes.push(m);
    listaEl.appendChild(crearBurbuja(m));
    guardar();
    bajarScroll();
  }

  function mostrarEscribiendo() {
    var el = document.createElement("div");
    el.className = "cb-msg cb-msg-bot cb-escribiendo";
    el.textContent = "Escribiendo…";
    listaEl.appendChild(el);
    bajarScroll();
    return el;
  }

  function setOcupado(valor) {
    ocupado = valor;
    enviarEl.disabled = valor;
    entradaEl.disabled = valor;
  }

  // ---------- envío ----------

  function historialParaEnviar() {
    return mensajes
      .filter(function (m) {
        return !m.error;
      })
      .slice(-MAX_ENVIADOS)
      .map(function (m) {
        return { rol: m.rol, contenido: m.contenido };
      });
  }

  async function enviar() {
    var pregunta = entradaEl.value.trim();
    if (!pregunta || ocupado) return;

    var historial = historialParaEnviar(); // antes de agregar la pregunta actual
    agregar({ rol: "user", contenido: pregunta });
    entradaEl.value = "";
    autoajustar();
    setOcupado(true);
    var indicador = mostrarEscribiendo();

    try {
      var resp = await fetch(URL_PREGUNTAR, {
        method: "POST",
        credentials: "same-origin",
        headers: Object.assign({ "Content-Type": "application/json" }, cabecerasAuth()),
        body: JSON.stringify({ pregunta: pregunta, historial: historial }),
      });

      var datos = null;
      try {
        datos = await resp.json();
      } catch (e) {}

      if (!resp.ok) {
        var msg;
        if (resp.status === 401 || resp.status === 403) {
          msg = "Tu sesión expiró o no tienes permiso. Inicia sesión de nuevo.";
        } else if (resp.status === 422) {
          msg = "Revisa tu pregunta (máximo 2000 caracteres).";
        } else if (datos && typeof datos.detail === "string") {
          msg = datos.detail;
        } else {
          msg = "Error " + resp.status + " al consultar el asistente.";
        }
        agregar({ rol: "assistant", contenido: msg, error: true });
      } else {
        agregar({
          rol: "assistant",
          contenido: (datos && datos.respuesta) || "Sin respuesta.",
          fuentes: (datos && datos.fuentes) || [],
        });
      }
    } catch (e) {
      agregar({
        rol: "assistant",
        contenido: "No se pudo conectar con el servidor.",
        error: true,
      });
    } finally {
      if (indicador.parentNode) indicador.parentNode.removeChild(indicador);
      setOcupado(false);
      entradaEl.focus();
    }
  }

  // ---------- panel ----------

  function abrir() {
    panel.hidden = false;
    raiz.classList.add("cb-abierto");
    boton.setAttribute("aria-expanded", "true");
    bajarScroll();
    entradaEl.focus();
  }

  function cerrar() {
    panel.hidden = true;
    raiz.classList.remove("cb-abierto");
    boton.setAttribute("aria-expanded", "false");
    boton.focus();
  }

  function alternar() {
    if (panel.hidden) abrir();
    else cerrar();
  }

  function limpiar() {
    mensajes = [];
    guardar();
    pintarTodo();
  }

  function autoajustar() {
    entradaEl.style.height = "auto";
    entradaEl.style.height = Math.min(entradaEl.scrollHeight, 120) + "px";
  }

  // ---------- montaje ----------

  function iniciar() {
    raiz = document.createElement("div");
    raiz.id = "cb-root";
    raiz.innerHTML =
      '<button class="cb-boton" type="button" aria-label="Abrir asistente" aria-expanded="false">' +
      icono("chat") +
      "</button>" +
      '<section class="cb-panel" role="dialog" aria-label="Asistente de documentación" hidden>' +
      '  <header class="cb-encabezado">' +
      '    <span class="cb-titulo">Asistente</span>' +
      '    <div class="cb-acciones">' +
      '      <button class="cb-limpiar" type="button" title="Nueva conversación" aria-label="Nueva conversación">' +
      icono("nuevo") +
      "</button>" +
      '      <button class="cb-cerrar" type="button" title="Cerrar" aria-label="Cerrar asistente">' +
      icono("cerrar") +
      "</button>" +
      "    </div>" +
      "  </header>" +
      '  <div class="cb-mensajes" aria-live="polite"></div>' +
      '  <div class="cb-entrada-caja">' +
      '    <textarea class="cb-entrada" rows="1" maxlength="2000" placeholder="Escribe tu pregunta…" aria-label="Pregunta"></textarea>' +
      '    <button class="cb-enviar" type="button">Enviar ' +
      icono("enviar") +
      "</button>" +
      "  </div>" +
      "</section>";
    document.body.appendChild(raiz);

    boton = raiz.querySelector(".cb-boton");
    panel = raiz.querySelector(".cb-panel");
    listaEl = raiz.querySelector(".cb-mensajes");
    entradaEl = raiz.querySelector(".cb-entrada");
    enviarEl = raiz.querySelector(".cb-enviar");

    boton.addEventListener("click", alternar);
    raiz.querySelector(".cb-cerrar").addEventListener("click", cerrar);
    raiz.querySelector(".cb-limpiar").addEventListener("click", limpiar);
    enviarEl.addEventListener("click", enviar);

    entradaEl.addEventListener("input", autoajustar);
    entradaEl.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        enviar();
      }
    });

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && !panel.hidden) cerrar();
    });

    mensajes = cargar();
    pintarTodo();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();