// Человекочитаемый текст ошибки из ответа API.
//
// DRF отвечает по-разному: {"detail": "..."} для явных ошибок и структуру по
// полям для валидации — {"items": [{"material": ["..."]}], "phone": ["..."]}.
// Раньше показывался только `detail`, а всё остальное схлопывалось в «Произошла
// ошибка», и понять причину можно было только в логах сервера.

function collect(node, out = []) {
  if (node == null) return out;
  if (typeof node === "string") {
    out.push(node);
    return out;
  }
  if (Array.isArray(node)) {
    node.forEach((x) => collect(x, out));
    return out;
  }
  if (typeof node === "object") {
    Object.values(node).forEach((x) => collect(x, out));
    return out;
  }
  return out;
}

export function apiError(e, fallback) {
  const data = e?.response?.data;
  if (data == null) {
    // Сервер не ответил вовсе — сеть, таймаут, упавший контейнер.
    return e?.message || fallback;
  }
  if (typeof data === "string") return data;
  if (data.detail) return data.detail;

  const parts = [...new Set(collect(data))];
  return parts.length ? parts.join(" ") : fallback;
}

export default apiError;
