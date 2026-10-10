/**
 * Ступени опта за лист (CLI-02, волна 2): «от 10 листов — 3 500, от 25 —
 * 3 200». Пара «опт / опт от» выше остаётся одной из ступеней; касса берёт
 * ступень с самым большим порогом, которого достиг заказ.
 */
import { useTranslation } from "react-i18next";

export default function PriceTiersEditor({ tiers, onChange, unit }) {
  const { t } = useTranslation();
  const rows = tiers || [];
  const set = (i, key, value) => onChange(rows.map((r, j) => (j === i ? { ...r, [key]: value } : r)));

  return (
    <div className="field">
      <label>{t("stock2.tiers")}</label>
      {rows.map((r, i) => (
        <div key={i} className="row" style={{ gap: 8, marginBottom: 6, alignItems: "center" }}>
          <span className="muted">{t("stock2.tierFrom")}</span>
          <input
            type="text" inputMode="decimal" style={{ width: 90 }} value={r.min_qty ?? ""}
            aria-label={t("stock2.tierFrom")}
            onChange={(e) => set(i, "min_qty", e.target.value)}
          />
          <span className="muted">{unit} —</span>
          <input
            type="text" inputMode="decimal" style={{ width: 110 }} value={r.price ?? ""}
            aria-label={t("stock2.tierPrice")}
            onChange={(e) => set(i, "price", e.target.value)}
          />
          <button type="button" className="ghost row-btn row-danger" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            ×
          </button>
        </div>
      ))}
      <button type="button" className="ghost" style={{ padding: 0, height: "auto", color: "var(--accent-ink)" }}
              onClick={() => onChange([...rows, { min_qty: "", price: "" }])}>
        + {t("stock2.addTier")}
      </button>
    </div>
  );
}
