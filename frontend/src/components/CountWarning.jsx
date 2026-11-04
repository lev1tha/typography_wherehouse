import { useTranslation } from "react-i18next";

import { formatMoney } from "../utils/format.js";

// Пересчёт обнуляет весь остаток — нужно отдельное «да» (прод 02.11: склад
// обнулили пересчётом по одной позиции, и месяц ушёл в убыток недостачей).
export const needsZeroConfirm = (cur, target) => target === 0 && cur > 0;

// Что пересчёт сделает с деньгами — до сохранения (прод 03.11: начальный
// остаток завели пересчётом «в плюс», и излишек 967 сом стал прибылью без
// единой продажи). Сумма — по средней цене остатка, иначе по закупочной из
// карточки; цены не видно (роль без закупочных) — только текст.
export default function CountWarning({ material, cur, target, confirmed, onConfirm }) {
  const { t } = useTranslation();
  if (!material || target == null || target < 0) return null;
  const delta = target - cur;
  if (Math.abs(delta) < 1e-9) return null;
  const value = Number(material.stock_value);
  const unitCost = cur > 0 && value > 0 ? value / cur : Number(material.purchase_price) || 0;
  const money = unitCost > 0 ? formatMoney(Math.abs(delta) * unitCost) : null;
  const zero = needsZeroConfirm(cur, target);

  let text;
  if (delta > 0) text = money ? t("countWarn.surplus", { money }) : t("countWarn.surplusNoMoney");
  else if (zero) text = money ? t("countWarn.zero", { money }) : t("countWarn.zeroNoMoney");
  else text = money ? t("countWarn.shortage", { money }) : t("countWarn.shortageNoMoney");

  return (
    <div
      className="card"
      role="note"
      style={{
        padding: 12,
        marginBottom: 14,
        fontSize: 13,
        background: delta > 0 || zero ? "var(--warn-bg)" : "var(--canvas)",
        color: delta > 0 || zero ? "var(--warn-ink)" : undefined,
      }}
    >
      <div>{text}</div>
      {delta > 0 && <div className="muted" style={{ marginTop: 4 }}>{t("countWarn.surplusHint")}</div>}
      {zero && (
        <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
          <input type="checkbox" checked={!!confirmed} onChange={(e) => onConfirm?.(e.target.checked)} />
          {t("countWarn.zeroConfirm")}
        </label>
      )}
    </div>
  );
}
