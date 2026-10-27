// api.js — funciones fetch reutilizables

const API_BASE = ""; // mismo origen: el backend sirve también el frontend

async function apiPost(url, formData) {
  const resp = await fetch(API_BASE + url, {
    method: "POST",
    body: formData,
  });

  const data = await resp.json().catch(() => null);

  if (!resp.ok) {
    const detalle = data && data.detail ? data.detail : `Error ${resp.status}`;
    throw new Error(detalle);
  }

  return data;
}

async function apiGet(url) {
  const resp = await fetch(API_BASE + url);
  const data = await resp.json().catch(() => null);

  if (!resp.ok) {
    const detalle = data && data.detail ? data.detail : `Error ${resp.status}`;
    throw new Error(detalle);
  }

  return data;
}
