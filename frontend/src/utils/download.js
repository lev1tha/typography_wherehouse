import api from "../api/api.js";

// Скачать файл, который отдаёт сервер (CSV «в Excel»). Через axios, а не
// ссылкой: запросу нужен токен в заголовке. Имя файла берётся из ответа сервера,
// `fallbackName` — на случай, если заголовок недоступен.
export async function downloadFile(url, params, fallbackName) {
  const r = await api.get(url, { params, responseType: "blob" });
  const disposition = r.headers?.["content-disposition"] || "";
  const name = /filename="?([^";]+)"?/.exec(disposition)?.[1] || fallbackName;
  const blob = new Blob([r.data], { type: r.headers?.["content-type"] || "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
