import { useTranslation } from "react-i18next";

// Таблица отчёта «строки — статьи, колонки — месяцы года + итог», как лист
// Excel заказчика. Общая для ОПиУ и ОДДС: у обоих строки приходят с сервера
// уже разложенными (`kind`: total / subtotal / group / row / percent / note /
// balance / grand), здесь только вид.
//
// Будущие месяцы текущего года — прочерком, а не нулём: «0» в ноябре, до
// которого ещё не дожили, читается как «ноябрь пустой».

const MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"];

export function money(n) {
  if (n === null || n === undefined) return "";
  const v = Math.round(Number(n) || 0);
  if (v === 0) return "0";
  return v.toLocaleString("ru-RU");
}

function cell(row, value) {
  if (value === null || value === undefined) return "";
  if (row.kind === "percent") return `${Number(value).toLocaleString("ru-RU")} %`;
  return money(value);
}

export default function StatementTable({ data }) {
  const { t } = useTranslation();
  const months = data.months || [];
  return (
    <div className="sheet-wrap stmt-wrap">
      <table className="sheet-table stmt-table">
        <thead>
          <tr>
            <th className="stmt-label">{t("statements.article")}</th>
            {months.map((m) => (
              <th key={m.month} className={m.future ? "stmt-future" : undefined}>
                <span className="sheet-num">{t(`statements.m${m.month}`, MONTHS_RU[m.month - 1])}</span>
              </th>
            ))}
            <th className="stmt-total-col">
              <span className="sheet-num">{t("statements.yearTotal")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row) => (
            <tr key={row.key} className={`stmt-${row.kind}`}>
              <td className="stmt-label" style={{ paddingLeft: 10 + row.level * 16 }}>
                {row.label}
              </td>
              {row.values.map((v, i) => (
                <td
                  key={i}
                  className={[
                    months[i]?.future ? "stmt-future" : "",
                    row.kind !== "percent" && Number(v) < 0 ? "stmt-neg" : "",
                  ].join(" ")}
                >
                  <span className="sheet-num">{months[i]?.future ? "—" : cell(row, v)}</span>
                </td>
              ))}
              <td className={`stmt-total-col ${row.kind !== "percent" && Number(row.total) < 0 ? "stmt-neg" : ""}`}>
                <span className="sheet-num">{cell(row, row.total)}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// CSV той же таблицы — владелец привык сверять в Excel. Разделитель «;» и
// BOM: так русский Excel открывает файл сразу по колонкам и без кракозябр.
export function statementCsv(data, title, t) {
  const head = [t("statements.article"), ...data.months.map((m) => MONTHS_RU[m.month - 1]), t("statements.yearTotal")];
  const lines = [head.join(";")];
  for (const row of data.rows) {
    const fmt = (v) => (v === null || v === undefined ? "" : row.kind === "percent" ? String(v) : String(Math.round(Number(v) || 0)));
    lines.push([`${"  ".repeat(row.level)}${row.label}`, ...row.values.map(fmt), fmt(row.total)].join(";"));
  }
  const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${title}-${data.year}.csv`;
  a.click();
}
