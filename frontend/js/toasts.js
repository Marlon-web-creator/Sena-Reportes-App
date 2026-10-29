// toasts.js — wrapper delgado sobre Toastify para mensajes consistentes

function notificar(mensaje, tipo = "info") {
  const colores = {
    success: "#2f9e6e",
    error: "#d9636d",
    info: "#39a900",
    warning: "#d99a2b",
  };

  Toastify({
    text: mensaje,
    duration: 4000,
    gravity: "top",
    position: "right",
    close: true,
    style: {
      background: colores[tipo] || colores.info,
      color: "#070d08",
      fontFamily: "Inter, system-ui, sans-serif",
      fontWeight: 500,
      borderRadius: "6px",
    },
  }).showToast();
}