import { formatNumber } from "./format.js";

// Подпись строки чека везде одна: «Резка лазером — акрил 3 мм клиента».
//
// У строки работы по материалу КЛИЕНТА и у гравировки своего материала нет,
// и без комментария в чеке стояла бы голая «Резка лазером × 12.5 пог.м» —
// через месяц не вспомнить, что это было. Комментарий дописывается через
// тире, «материал клиента» — пометкой в скобках, чтобы и на бумаге было
// видно, что со склада ничего не уходило.
export function itemTitle(it, t) {
  const base = (it.type === "SERVICE" ? it.service_name : it.material_name) || "—";
  const withNote = it.note ? `${base} — ${it.note}` : base;
  if (it.own_material && t) return `${withNote} (${t("checkout.ownCutTag")})`;
  return withNote;
}

// --- Подпись строки: размеры, детали, проходы, станок, материал работы -------
//
// После оформления заказа «Резка лазером × 14,5 пог.м» сама по себе ничего не
// говорит мастеру: из каких деталей эти метры, на каком станке и по какому
// материалу. Всё это хранится на строке (XL-11); здесь оно собирается в короткие
// куски, которые экран и печатная форма склеивают одинаково.
const dim = (n) => formatNumber(n, { max: 3 });

/** Строка работы резки: количество у неё — длина реза в пог.м (всего на все детали).
 * Отходы тоже бывают в метрах, но у них мерка записана в `sale_mode`. */
export const isCutLine = (it) =>
  it.type === "SERVICE" && it.unit_code === "METER" && !it.sale_mode;

/** Куски подписи: «0,455×0,320 м», «12 дет.», «14,5 пог.м», «2 проход.», станок,
 * материал работы. Пустой массив — подписывать нечем. */
export function itemSpecParts(it, t) {
  const out = [];
  const w = Number(it.width);
  const l = Number(it.length);
  if (w > 0 && l > 0) out.push(t("receiptsV2.spec.size", { w: dim(w), l: dim(l) }));
  if (Number(it.parts_count) > 1) out.push(t("receiptsV2.spec.parts", { n: it.parts_count }));
  if (isCutLine(it) && Number(it.quantity) > 0) {
    out.push(t("receiptsV2.spec.runM", { n: dim(it.quantity) }));
  }
  if (Number(it.passes) > 1) out.push(t("receiptsV2.spec.passes", { n: it.passes }));
  if (it.machine) out.push(t(`machine.${it.machine}`, { defaultValue: it.machine_display || it.machine }));
  if (it.work_material_name) out.push(t("receiptsV2.spec.material", { name: it.work_material_name }));
  return out;
}

/** «выдано 5 из 8 шт» — пока по строке что-то выдали (G1-N4); иначе пустая строка. */
export function issuedLabel(it, t) {
  if (!(Number(it.issued_qty) > 0)) return "";
  // Строка с деталями выдаётся штуками (перепроверка 10.10, RU-N3).
  if (Number(it.parts_count) > 1) {
    const done = Number(it.issued_qty) >= Number(it.quantity) ? Number(it.parts_count) : Number(it.issued_parts || 0);
    return t("receiptsV2.issuedOf", { done, total: it.parts_count, unit: t("issue.partsUnit") });
  }
  const unit = it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || "";
  return t("receiptsV2.issuedOf", { done: dim(it.issued_qty), total: dim(it.quantity), unit });
}
