// Правила прайса заказа (2026-10-10): как показать «каталог → итог» в чеке,
// в окне заказа и на печати. Считает сервер (`sales/pricing_rules.py`); здесь
// только подписи по полям чека и строк.
import { formatNumber } from "./format.js";

const num = (v) => Number(v) || 0;
// Вверх до сома, как на сервере; эпсилон гасит шум double.
const ceilSom = (v) => Math.max(0, Math.ceil((Number(v) || 0) - 1e-6));

/** Сумма строки по правилам — порядок как на сервере: расчёт → минимум (если
 * строка не бесплатная) → ×(1+срочность) → ×(1−скидка) → вверх до сома. */
export function applyRules(raw, { minimum = 0, urgency = 0, discount = 0 } = {}) {
  let amount = num(raw);
  if (num(minimum) > 0 && amount > 0 && amount < num(minimum)) amount = num(minimum);
  amount = (amount * (100 + num(urgency))) / 100;
  amount = (amount * (100 - num(discount))) / 100;
  return ceilSom(amount);
}

/** Правила, записанные на строке чека (для пересчёта в окне правки). */
export function itemRules(item) {
  return {
    minimum: item.min_amount,
    urgency: item.urgency_percent,
    discount: item.discount_percent,
  };
}

/** Строку изменили правила прайса (минимум, срочность, скидка)? */
export function lineRuled(item) {
  return item.catalog_total != null && num(item.catalog_total) !== num(item.sold_total ?? item.line_total);
}

/** В заказе есть строки, которые изменили правила прайса? */
export function receiptRuled(receipt) {
  return (receipt?.items || []).some(lineRuled);
}

/** «Срочность +25 % · Скидка 5 % · Минимум строки» — что сработало в заказе. */
export function rulesLabel(receipt, t) {
  if (!receipt) return "";
  const parts = [];
  if (receipt.is_urgent && num(receipt.urgency_percent) > 0)
    parts.push(t("checkout.rulesUrgency", { n: formatNumber(receipt.urgency_percent, { max: 2 }) }));
  if (num(receipt.discount_percent) > 0)
    parts.push(t("checkout.rulesDiscount", { n: formatNumber(receipt.discount_percent, { max: 2 }) }));
  if ((receipt.items || []).some((i) => i.min_applied)) parts.push(t("checkout.rulesMinimum"));
  return parts.join(" · ");
}
