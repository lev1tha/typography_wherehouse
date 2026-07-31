import { useTranslation } from "react-i18next";

// Выбор месяца стрелками ‹ ›, как в графике прибыли. Значение — {year, month},
// month человеческий (1–12). Кнопка «Все» снимает фильтр (month = null).
export default function MonthPicker({ value, onChange, allowAll = true }) {
  const { t } = useTranslation();
  const MONTHS = t("months", { returnObjects: true });
  const now = new Date();
  const year = value?.year ?? now.getFullYear();
  const month = value?.month ?? null;

  function shift(delta) {
    // Без выбранного месяца стрелка начинает с текущего.
    const base = month ?? now.getMonth() + 1;
    const raw = base + delta;
    if (raw < 1) return onChange({ year: year - 1, month: 12 });
    if (raw > 12) return onChange({ year: year + 1, month: 1 });
    onChange({ year, month: raw });
  }

  const label = month
    ? `${Array.isArray(MONTHS) ? MONTHS[month - 1] : month} ${year}`
    : t("common.all");

  return (
    <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
      <button className="ghost" onClick={() => shift(-1)} aria-label="‹">‹</button>
      <strong style={{ minWidth: 130, textAlign: "center" }}>{label}</strong>
      <button className="ghost" onClick={() => shift(1)} aria-label="›">›</button>
      {allowAll && month && (
        <button className="ghost" onClick={() => onChange({ year, month: null })}>
          {t("common.reset")}
        </button>
      )}
    </div>
  );
}
