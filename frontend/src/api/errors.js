// Человекочитаемый текст ошибки из ответа API.
//
// DRF отвечает по-разному: {"detail": "..."} для явных ошибок и структуру по
// полям для валидации — {"items": [{"material": ["..."]}], "phone": ["..."]}.
// Раньше показывался только `detail`, а всё остальное схлопывалось в «Произошла
// ошибка», и понять причину можно было только в логах сервера.
import i18n from "../i18n";

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

/** Вид сбоя: что именно произошло, независимо от текста сервера. */
export function failureKind(e) {
  if (!e) return "unknown";
  if (e.code === "ECONNABORTED" || e.code === "ETIMEDOUT") return "timeout";
  const status = e.response?.status;
  if (!e.response) return "network"; // сервер не ответил вовсе
  if (status === 429) return "rate";
  if (status === 403) return "forbidden";
  if (status >= 500) return "server";
  return "client";
}

export function apiError(e, fallback) {
  const t = (k) => i18n.t(k);
  switch (failureKind(e)) {
    case "timeout":
      return t("errors.timeout");
    case "network":
      return t("errors.network");
    case "rate":
      return t("errors.rateLimit");
    case "server":
      // Тело 5xx — страница прокси или трейс, показывать его человеку нельзя.
      return t("errors.server");
    default:
      break;
  }
  const data = e?.response?.data;
  if (data == null) return fallback;
  // 403 без внятного detail — общее «нет прав» (DRF отдаёт английскую фразу).
  if (e.response.status === 403 && (!data.detail || /^[\x00-\x7f]+$/.test(String(data.detail)))) {
    return t("errors.forbidden");
  }
  if (typeof data === "string") return data.trim().startsWith("<") ? fallback : data;
  if (data.detail) return data.detail;

  const parts = [...new Set(collect(data))];
  return parts.length ? parts.join(" ") : fallback;
}

export default apiError;
