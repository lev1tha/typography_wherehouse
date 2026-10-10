/**
 * Наценка от закупа в карточке материала (STK-03, волна 2).
 *
 * Цена продажи вводится руками, как и раньше. Здесь — закуп ПОСЛЕДНЕЙ партии в
 * единицах продажи, цена по наценке («подставить» кладёт её в поля цены) и
 * красная строка, если введённая цена ниже закупа: пришла партия дороже —
 * карточка продолжала продавать по-старому, и маржу −1 400 было видно только
 * в чеке постфактум.
 */
import { useTranslation } from "react-i18next";

import { formatMoney } from "../utils/format.js";
import { parseNumber } from "../utils/pasteTable.js";

// Поле цены карточки для каждой единицы закупа.
const PRICE_FIELD = { sqm: "price_per_sqm", sheet: "piece_price", pm: "price_per_pm", unit: "price_per_unit" };

const ceilSom = (n) => Math.ceil(Math.round(n * 100) / 100);

export default function MarkupHint({ editing, setEditing }) {
  const { t } = useTranslation();
  const cost = editing.pricing?.cost || {};
  const markup = parseNumber(editing.markup_percent);
  const units = Object.keys(cost).filter((k) => PRICE_FIELD[k]);
  const suggested = Object.fromEntries(
    units.map((k) => [k, markup == null ? null : ceilSom(Number(cost[k]) * (1 + markup / 100))])
  );
  const unitLabel = (k) =>
    k === "sheet" ? t("warehouse.unitSheet") : k === "pm" ? t("unit.METER") : k === "sqm" ? t("unit.SQM") : t(`unit.${editing.unit || "PIECE"}`);
  // Цена ниже закупа — по тому, что введено в форме сейчас.
  const below = units.filter((k) => {
    const price = Number(editing[PRICE_FIELD[k]]) || 0;
    return price > 0 && price < Number(cost[k]);
  });

  function apply() {
    const next = { ...editing };
    units.forEach((k) => {
      if (suggested[k] != null) next[PRICE_FIELD[k]] = String(suggested[k]);
    });
    setEditing(next);
  }

  return (
    <div className="field">
      <label>{t("stock2.markup")}</label>
      <input
        type="text"
        inputMode="decimal"
        value={editing.markup_percent ?? ""}
        placeholder={t("stock2.markupPh")}
        onChange={(e) => setEditing({ ...editing, markup_percent: e.target.value })}
      />
      {units.length > 0 ? (
        <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
          {t("stock2.lastCost")}:{" "}
          {units.map((k, i) => (
            <span key={k}>{i > 0 ? " · " : ""}{formatMoney(cost[k], { fraction: 2 })}/{unitLabel(k)}</span>
          ))}
          {markup != null && (
            <>
              <br />
              {t("stock2.byMarkup", { value: markup })}:{" "}
              {units.map((k, i) => (
                <span key={k}>{i > 0 ? " · " : ""}<strong>{formatMoney(suggested[k])}</strong>/{unitLabel(k)}</span>
              ))}{" "}
              <button
                type="button"
                className="ghost"
                style={{ color: "var(--accent-ink)", padding: 0, height: "auto" }}
                onClick={apply}
              >
                {t("stock2.applySuggested")}
              </button>
            </>
          )}
        </p>
      ) : (
        editing.id != null && <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>{t("stock2.noCost")}</p>
      )}
      {below.length > 0 && (
        <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "4px 0 0" }}>
          {t("stock2.belowCost", { units: below.map(unitLabel).join(", ") })}
        </p>
      )}
    </div>
  );
}
