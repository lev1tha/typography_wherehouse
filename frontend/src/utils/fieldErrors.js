// Ошибки валидации DRF по полям: {"amount": ["…"], "note": ["…"]} → {amount: "…"}.
//
// Тост на 3 секунды не говорит, КАКОЕ поле неверно; для форм с `<Field error>`
// достаточно разложить ответ по именам полей. Ответы вида {"detail": "…"} и не-400
// сюда не относятся — их показывает `apiError`.
export function fieldErrors(e) {
  const data = e?.response?.data;
  if (e?.response?.status !== 400 || !data || typeof data !== "object" || Array.isArray(data)) return {};
  const out = {};
  for (const [key, value] of Object.entries(data)) {
    if (key === "detail") continue;
    const text = [].concat(value).filter((x) => typeof x === "string").join(" ");
    if (text) out[key] = text;
  }
  return out;
}
