import { Fragment } from "react";
import { useTranslation } from "react-i18next";

import Hint from "./Hint.jsx";
import { formatNumber } from "../utils/format.js";

// Таблица отчёта «строки — статьи, колонки — месяцы года + итог», как лист
// Excel заказчика. Общая для ОПиУ и ОДДС: у обоих строки приходят с сервера
// уже разложенными (`kind`: total / subtotal / group / row / percent / note /
// balance / grand / warn), здесь только вид. У строки может быть `hint` —
// ключ подсказки к термину (`terms.<hint>`) — и `warn` — предупреждение.
//
// Будущие месяцы текущего года — прочерком, а не нулём: «0» в ноябре, до
// которого ещё не дожили, читается как «ноябрь пустой».

const MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"];

export function money(n) {
  if (n === null || n === undefined) return "";
  const v = Math.round(Number(n) || 0);
  if (v === 0) return "0";
  return formatNumber(v);
}

// Подпись строки: постоянные строки отчётов переведены по ключу
// (`stmtRows.<key>`), строки видов расхода — их названия из справочника, как
// пришли с сервера. Ставка налога берётся из серверной подписи.
// Ключи разнесены по отчётам (`pnl` / `cf` / `bridge`): «net» в ОПиУ —
// чистая прибыль, в ОДДС — чистый денежный поток. Двоеточие в ключе i18next
// принял бы за пространство имён — заменяем на «__».
export function rowLabel(row, t, kind) {
  const rate = (String(row.label).match(/([\d.,]+)\s*%/) || [])[1];
  const key = String(row.key).replace(/:/g, "__");
  return t(`stmtRows.${kind}.${key}`, { defaultValue: row.label, rate });
}

// «Строка-предупреждение»: несведённые переводы и «Не объяснено» в сверке.
// Ноль — спокойно серым, не ноль — красным.
function warnClass(row) {
  if (row.kind !== "warn" && !row.warn) return "";
  const any = row.values.some((v) => Math.round(Number(v) || 0) !== 0);
  return any ? "stmt-warn" : "stmt-warn-zero";
}

function cell(row, value) {
  if (value === null || value === undefined) return "";
  if (row.kind === "percent") return `${formatNumber(value)} %`;
  return money(value);
}

// Квартал, целиком лежащий в будущем, — прочерком; начавшийся — числами (в нём
// уже есть месяцы с данными).
const quarterOf = (data, q) => (data.quarters || [])[q];

function i18nHas(t, key) {
  return t(key, { defaultValue: "" }) !== "";
}

export default function StatementTable({ data, kind, showQuarters = false }) {
  const { t } = useTranslation();
  const months = data.months || [];
  const withQuarters = showQuarters && (data.quarters || []).length === 4;
  return (
    <div className="sheet-wrap stmt-wrap">
      <table className="sheet-table stmt-table">
        <thead>
          <tr>
            <th className="stmt-label">{t("statements.article")}</th>
            {months.map((m, i) => (
              <Fragment key={m.month}>
                <th className={m.future ? "stmt-future" : undefined}>
                  <span className="sheet-num">{t(`statements.m${m.month}`, MONTHS_RU[m.month - 1])}</span>
                </th>
                {withQuarters && i % 3 === 2 && (
                  <th className="stmt-quarter-col">
                    <span className="sheet-num">{t("statements.quarterShort", { n: Math.floor(i / 3) + 1 })}</span>
                  </th>
                )}
              </Fragment>
            ))}
            <th className="stmt-total-col">
              <span className="sheet-num">{t("statements.yearTotal")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row) => (
            <tr key={row.key} className={`stmt-${row.kind} ${warnClass(row)}`}>
              <td className="stmt-label" style={{ paddingLeft: 10 + row.level * 16 }}>
                {rowLabel(row, t, kind)}
                {row.hint && i18nHas(t, `terms.${row.hint}`) && <Hint text={t(`terms.${row.hint}`)} />}
                {row.warn && <Hint tone="warn" text={t("statements.unmatchedWarn", { defaultValue: row.warn })} />}
              </td>
              {row.values.map((v, i) => (
                <Fragment key={i}>
                  <td
                    className={[
                      months[i]?.future ? "stmt-future" : "",
                      row.kind !== "percent" && Number(v) < 0 ? "stmt-neg" : "",
                    ].join(" ")}
                  >
                    <span className="sheet-num">{months[i]?.future ? "—" : cell(row, v)}</span>
                  </td>
                  {withQuarters && i % 3 === 2 && (() => {
                    const q = Math.floor(i / 3);
                    const qv = (row.quarters || [])[q];
                    return (
                      <td className={`stmt-quarter-col ${row.kind !== "percent" && Number(qv) < 0 ? "stmt-neg" : ""}`}>
                        <span className="sheet-num">{quarterOf(data, q)?.future ? "—" : cell(row, qv)}</span>
                      </td>
                    );
                  })()}
                </Fragment>
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
export function statementCsv(data, title, t, kind, withQuarters = false) {
  const quarters = withQuarters && (data.quarters || []).length === 4;
  const monthHead = [];
  data.months.forEach((m, i) => {
    monthHead.push(MONTHS_RU[m.month - 1]);
    if (quarters && i % 3 === 2) monthHead.push(t("statements.quarterShort", { n: Math.floor(i / 3) + 1 }));
  });
  const head = [t("statements.article"), ...monthHead, t("statements.yearTotal")];
  const lines = [head.join(";")];
  for (const row of data.rows) {
    const fmt = (v) => (v === null || v === undefined ? "" : row.kind === "percent" ? String(v) : String(Math.round(Number(v) || 0)));
    const cells = [];
    row.values.forEach((v, i) => {
      cells.push(fmt(v));
      if (quarters && i % 3 === 2) cells.push(fmt((row.quarters || [])[Math.floor(i / 3)]));
    });
    lines.push([`${"  ".repeat(row.level)}${rowLabel(row, t, kind)}`, ...cells, fmt(row.total)].join(";"));
  }
  const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${title}-${data.year}.csv`;
  a.click();
}
