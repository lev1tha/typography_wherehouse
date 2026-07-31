import { useTranslation } from "react-i18next";

// Выбор месяца: список месяцев + год, со стрелками ‹ › для быстрого перехода.
// Раньше был только текст между стрелками — было непонятно, что это выбор
// месяца и что месяц вообще можно выбрать напрямую.
// Значение — {year, month}, month человеческий (1–12); month = null → «Все».
export default function MonthPicker({ value, onChange, label, years = 3 }) {
  const { t } = useTranslation();
  const MONTHS = t("months", { returnObjects: true });
  const names = Array.isArray(MONTHS) ? MONTHS : [];
  const now = new Date();
  const year = value?.year ?? now.getFullYear();
  const month = value?.month ?? null;

  // Год выбираем из нескольких последних — заказы за 2019-й искать некому.
  const yearOptions = [];
  for (let y = now.getFullYear(); y > now.getFullYear() - years; y--) yearOptions.push(y);
  if (!yearOptions.includes(year)) yearOptions.push(year);

  function shift(delta) {
    // Без выбранного месяца стрелка начинает с текущего.
    const base = month ?? now.getMonth() + 1;
    const raw = base + delta;
    if (raw < 1) return onChange({ year: year - 1, month: 12 });
    if (raw > 12) return onChange({ year: year + 1, month: 1 });
    onChange({ year, month: raw });
  }

  return (
    <div className="field" style={{ margin: 0 }}>
      <label>{label ?? t("common.month")}</label>
      {/* nowrap и явные ширины: в .toolbar у select'ов min-width 150px, и без
          этого стрелки с годом переносились на отдельные строки. */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "nowrap" }}>
        <button className="ghost" onClick={() => shift(-1)} aria-label={t("common.prevMonth")}>‹</button>
        <select
          value={month ?? ""}
          onChange={(e) => onChange({ year, month: e.target.value ? Number(e.target.value) : null })}
          style={{ minWidth: 0, width: 140 }}
        >
          <option value="">{t("common.allMonths")}</option>
          {names.map((n, i) => (
            <option key={n} value={i + 1}>{n}</option>
          ))}
        </select>
        <select
          value={year}
          onChange={(e) => onChange({ year: Number(e.target.value), month })}
          style={{ minWidth: 0, width: 90 }}
        >
          {yearOptions.sort((a, b) => b - a).map((y) => (
            <option key={y} value={y}>{y}</option>
          ))}
        </select>
        <button className="ghost" onClick={() => shift(1)} aria-label={t("common.nextMonth")}>›</button>
      </div>
    </div>
  );
}
