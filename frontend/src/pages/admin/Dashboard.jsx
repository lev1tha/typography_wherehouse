import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";

const COLORS = ["#e8853a", "#ffc592", "#2a9d99", "#d6b6f6", "#7a4a1e", "#1aae39"];

// Расходы финотчёта одним списком — те виды, что уменьшают прибыль. Вложения
// (оборудование, улучшение цеха) идут отдельной карточкой, поэтому здесь их нет.
//
// Блок «Материалы» идёт ОДНОЙ строкой — своим итогом, а не составляющими.
// Раньше сюда высыпались его входные данные (закуп, транспорт, долг), и рядом
// с итогом «Расходы 265 092» стояла строка «Закуп материала 1 177 792»:
// со стороны это выглядит арифметической ошибкой, хотя в прибыль идёт итог
// блока (начало + закуп + транспорт − конец), а не закуп сам по себе.
const expenseRows = (fin, materialsLabel) => [
  ...(Number(fin.materials?.total)
    ? [{ id: "materials", name: materialsLabel, amount: fin.materials.total }]
    : []),
  ...[...(fin.fixed?.rows || []), ...(fin.variable?.rows || [])].filter((r) => r.in_profit),
];

// Количества без хвоста нулей и с разрядами — как в каталоге («2», «0», «14,88»).
const qtyFmt = (v) => Number(v || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });

function Stat({ label, value, suffix, color, sub }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value" style={color ? { color: `var(--${color})` } : undefined}>
        {value}
        {suffix ? <span className="muted" style={{ fontSize: "1rem" }}> {suffix}</span> : null}
      </div>
      {/* Формула прямо под цифрой. Она есть и отдельным блоком ниже, но плитка
          стоит на первом экране, а блок уходит под сгиб — и цифра читается как
          «просто сумма продажи», непонятно откуда взявшаяся. */}
      {sub ? <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{sub}</div> : null}
    </div>
  );
}

// Dependency-free SVG donut.
function Donut({ segments }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  const r = 52;
  const c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <svg width="130" height="130" viewBox="0 0 130 130">
      <g transform="translate(65,65) rotate(-90)">
        <circle r={r} fill="none" stroke="var(--canvas)" strokeWidth="16" />
        {total > 0 &&
          segments.map((seg, i) => {
            const frac = seg.value / total;
            const dash = frac * c;
            const el = (
              <circle
                key={i}
                r={r}
                fill="none"
                stroke={seg.color}
                strokeWidth="16"
                strokeDasharray={`${dash} ${c - dash}`}
                strokeDashoffset={-offset}
              />
            );
            offset += dash;
            return el;
          })}
      </g>
    </svg>
  );
}

export default function Dashboard() {
  const { t } = useTranslation();
  const [data, setData] = useState(null);
  const [materials, setMaterials] = useState([]);
  const [clientBuys, setClientBuys] = useState([]);
  const [fin, setFin] = useState(null);
  const [error, setError] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  function loadDashboard() {
    const params = {};
    if (from) params.date_from = from;
    if (to) params.date_to = to;
    api.get("/audit/dashboard/", { params }).then((r) => setData(r.data)).catch(() => setError(t("common.error")));
    // Покупки по клиентам — за тот же период и на той же базе, что «Продали
    // материала на …» выше: иначе сумма таблицы не сходилась с плиткой.
    api.get("/audit/client-purchases/", { params: { ordering: "-material_spend", ...params } })
      .then((r) => setClientBuys(r.data)).catch(() => {});
    // Финотчёт (расходы/вложения/прибыль) тоже слушает период — те же даты.
    api.get("/finance/report/", { params }).then((r) => setFin(r.data)).catch(() => {});
  }

  useEffect(() => {
    // page_size: без него в разбивку попадали первые 25 материалов из каталога,
    // и сумма по типам не сходилась со «Стоимостью склада» вверху той же страницы.
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results));
  }, []);

  useEffect(() => {
    loadDashboard();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from, to]);

  // Группируем по ТИПУ материала. Раньше группировка шла по `m.category` —
  // полю, которое удалили, когда номенклатуру разобрали на отдельные поля.
  // Все материалы попадали в один ключ, и на видном месте финансового отчёта
  // висела подпись «undefined».
  const byCategory = useMemo(() => {
    const map = {};
    for (const m of materials) {
      const key = m.type_name || t("dashboard.noType");
      map[key] = (map[key] || 0) + Number(m.stock_value || 0);
    }
    return Object.entries(map)
      .map(([category, value]) => ({ category, value }))
      .filter((row) => row.value > 0)
      .sort((a, b) => b.value - a.value);
  }, [materials, t]);

  if (error) return <div className="error">{error}</div>;
  if (!data) return <p className="muted">{t("common.loading")}</p>;

  const som = (v) => `${Math.round(Number(v) || 0).toLocaleString("ru-RU")} сом`;
  const rev = data.revenue;
  const revTotal = Number(rev.total);
  const maxCat = Math.max(1, ...byCategory.map((x) => x.value));

  const methods = [
    { key: "cash", label: t("checkout.cash"), color: COLORS[0] },
    { key: "mbank", label: t("checkout.mbank"), color: COLORS[1] },
    { key: "demirbank", label: t("checkout.demirbank"), color: COLORS[2] },
    { key: "online", label: t("checkout.online"), color: COLORS[3] },
  ];

  function downloadCsv() {
    const lines = [];
    const push = (k, v) => lines.push(`${k};${v}`);
    push(t("dashboard.period"), `${from || "…"} — ${to || "…"}`);
    push("", "");
    methods.forEach((m) => push(m.label, Math.round(Number(rev[m.key]))));
    push(t("dashboard.revenueTotal"), Math.round(revTotal));
    if (data.breakdown) {
      push(t("dashboard.workRevenue"), Math.round(Number(data.breakdown.work_revenue)));
      push(t("dashboard.materialRevenue"), Math.round(Number(data.breakdown.material_revenue)));
      push(t("dashboard.materialCost"), Math.round(Number(data.breakdown.material_cost)));
      push(t("dashboard.materialProfit"), Math.round(Number(data.breakdown.material_profit)));
    }
    push(t("dashboard.services"), data.services_performed);
    push(t("dashboard.refunded"), Math.round(Number(data.refunds.total_refunded)));
    if (fin) {
      push("", "");
      // Строки — виды расхода из отчёта, поэтому свои виды («Реклама»,
      // «Налоги») попадают в выгрузку сами, без правки этого списка.
      for (const row of expenseRows(fin, t("finance.materialsBlock"))) push(row.name, Math.round(Number(row.amount)));
      push(t("finance.cogs"), Math.round(Number(fin.cogs)));
      push(t("finance.grossMargin"), Math.round(Number(fin.gross_margin)));
      push(t("finance.expenses"), Math.round(Number(fin.total_expenses)));
      push(t("finance.investmentsTotal"), Math.round(Number(fin.investments.total)));
      push(t("finance.profit"), Math.round(Number(fin.profit)));
      push(t("finance.clientDebt"), Math.round(Number(fin.client_debt)));
    }
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "obzor.csv";
    a.click();
  }

  return (
    <>
      <h1>{t("dashboard.title")}</h1>

      {/* Фильтр периода + экспорт */}
      <div className="toolbar" style={{ alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("dashboard.from")}</label>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("dashboard.to")}</label>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
        {(from || to) && (
          <button className="ghost" onClick={() => { setFrom(""); setTo(""); }}>{t("common.reset")}</button>
        )}
        <div style={{ flex: 1 }} />
        <button className="secondary" onClick={downloadCsv}>{t("finance.downloadCsv")}</button>
      </div>

      <div className="stat-grid" style={{ marginTop: 12 }}>
        <Stat label={t("dashboard.asset")} value={som(data.unrealised_asset)} />
        <Stat label={t("dashboard.revenueTotal")} value={som(revTotal)} />
        <Stat label={t("dashboard.services")} value={data.services_performed} />
        <Stat label={t("dashboard.refunded")} value={som(data.refunds.total_refunded)} />
        {/* Как в каталоге: красное — остаток есть, но упал до порога; ноль —
            отдельной строкой, спокойно (свежий каталог весь на нуле). */}
        <Stat
          label={t("dashboard.lowStock")}
          value={data.low_stock_count}
          // «Нет в наличии» — состояние тяжелее, чем «на исходе», и оно тоже
          // должно светиться. Раньше при 0 на исходе и 14 позициях с нулевым
          // остатком плитка выглядела спокойной: тревожный цвет включался
          // только по low_stock_count.
          color={
            data.low_stock_count > 0 || data.out_of_stock_count > 0
              ? "danger"
              : undefined
          }
          sub={data.out_of_stock_count > 0 ? t("dashboard.outOfStockSub", { n: data.out_of_stock_count }) : undefined}
        />
      </div>

      {/* Работа и материал. У материала одной цифры мало: «продали на 149 232»
          не отвечает, сколько на нём заработали, пока рядом не стоит, почём он
          нам достался. Поэтому материал показан формулой, а не плиткой. */}
      {data.breakdown && (
        <>
          <div className="stat-grid">
            {/* Прибыль до расходов — работа плюс заработок на материале.
                Выручка наверху осталась выручкой (клиенты правда заплатили
                5 525), но читалась как заработок; здесь видно, сколько из неё
                своё, а сколько — деньги, за которые материал куплен. */}
            <Stat
              label={t("dashboard.profitBeforeExpenses")}
              value={som(data.breakdown.profit_before_expenses)}
              color={Number(data.breakdown.profit_before_expenses) >= 0 ? "ok" : "danger"}
              sub={t("dashboard.profitBeforeExpensesFormula", {
                revenue: som(revTotal),
                cogs: som(data.breakdown.cogs_total),
              })}
            />
            <Stat label={t("dashboard.workRevenue")} value={som(data.breakdown.work_revenue)} />
            <Stat
              label={t("dashboard.materialProfit")}
              value={som(data.breakdown.material_profit)}
              color={Number(data.breakdown.material_profit) >= 0 ? "ok" : "danger"}
              sub={t("dashboard.materialProfitFormula", {
                revenue: som(data.breakdown.material_revenue),
                cost: som(data.breakdown.material_cost),
              })}
            />
          </div>

          <div className="card" style={{ marginTop: 12 }}>
            <h3>{t("dashboard.materialProfitTitle")}</h3>
            <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>
              {t("dashboard.materialProfitHint")}
            </p>
            <div className="crow">
              <span className="k">{t("dashboard.materialRevenue")}</span>
              <span>{som(data.breakdown.material_revenue)}</span>
            </div>
            <div className="crow">
              <span className="k">{t("dashboard.materialCost")}</span>
              <span style={{ color: "var(--danger)" }}>− {som(data.breakdown.material_cost)}</span>
            </div>
            <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6, paddingTop: 8 }}>
              <strong style={{ color: "var(--accent-strong)" }}>{t("dashboard.materialProfit")}</strong>
              <strong style={{ color: "var(--accent-strong)" }}>{som(data.breakdown.material_profit)}</strong>
            </div>
          </div>
        </>
      )}

      {fin && (
        <>
          <div className="stat-grid">
            <Stat
              label={t("finance.clientDebt")}
              value={som(fin.client_debt)}
              color={Number(fin.client_debt) > 0 ? "danger" : undefined}
            />
            <Stat label={t("finance.expenses")} value={som(fin.total_expenses)} />
            <Stat
              label={t("finance.profit")}
              value={som(fin.profit)}
              color={Number(fin.profit) >= 0 ? "ok" : "danger"}
            />
          </div>

          <div className="chart-row">
            {/* Расходы детально */}
            <div className="card">
              <h3>{t("dashboard.expensesBreakdown")}</h3>
              {expenseRows(fin, t("finance.materialsBlock")).map((row) => (
                <div className="crow" key={row.id}>
                  <span className="k">{row.name}</span><span>{som(row.amount)}</span>
                </div>
              ))}
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6, paddingTop: 8 }}>
                <strong style={{ color: "var(--accent-strong)" }}>{t("finance.expenses")}</strong>
                <strong style={{ color: "var(--accent-strong)" }}>{som(fin.total_expenses)}</strong>
              </div>
              {/* Себестоимость проданного — справочная, в итог не входит (материал
                  уже посчитан закупом). Раньше стояла строкой ВНУТРИ списка, и
                  список читался как сумма, которой не был. */}
              <div className="crow" style={{ paddingTop: 8 }}>
                <span className="muted" style={{ fontSize: 13 }}>{t("dashboard.cogsAside")}</span>
                <span className="muted" style={{ fontSize: 13 }}>{som(fin.cogs)}</span>
              </div>
            </div>

            {/* Вложения (не в прибыль) */}
            <div className="card">
              <h3>{t("finance.investmentsTitle")}</h3>
              <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.investmentsHint")}</p>
              {(fin.investments.rows || []).map((row) => (
                <div className="crow" key={row.id}>
                  <span className="k">{row.name}</span><span>{som(row.amount)}</span>
                </div>
              ))}
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6, paddingTop: 8 }}>
                <strong style={{ color: "var(--accent-strong)" }}>{t("finance.investmentsTotal")}</strong>
                <strong style={{ color: "var(--accent-strong)" }}>{som(fin.investments.total)}</strong>
              </div>
            </div>
          </div>
        </>
      )}

      <div className="chart-row">
        {/* Revenue split donut (by payment method) */}
        <div className="card">
          <h3>{t("dashboard.revenueSplit")}</h3>
          {revTotal > 0 ? (
            <div className="donut-wrap">
              <Donut segments={methods.map((m) => ({ value: Number(rev[m.key]), color: m.color }))} />
              <div className="legend">
                {methods.map((m) => (
                  <div className="lg" key={m.key}>
                    <span className="dot" style={{ background: m.color }} />
                    {m.label}: <strong>{som(rev[m.key])}</strong>
                    <span className="muted">({Math.round((Number(rev[m.key]) / revTotal) * 100)}%)</span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="muted">{t("common.empty")}</p>
          )}
        </div>

        {/* Stock value by category bars */}
        <div className="card">
          <h3>{t("dashboard.stockByCategory")}</h3>
          {byCategory.length ? (
            byCategory.map((row, i) => (
              <div className="bar-row" key={row.category}>
                <div className="bar-head">
                  <span>{row.category}</span>
                  <strong>{som(row.value)}</strong>
                </div>
                <div className="bar-track">
                  <div
                    className="bar-fill"
                    style={{ width: `${(row.value / maxCat) * 100}%`, background: COLORS[i % COLORS.length] }}
                  />
                </div>
              </div>
            ))
          ) : (
            <p className="muted">{t("common.empty")}</p>
          )}
        </div>
      </div>

      {/* Материалы на исходе */}
      {data.low_stock_items?.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>{t("dashboard.lowStockTitle")}</h3>
          <div className="table-scroll">
          <table className="table plain-table">
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("dashboard.remaining")}</th>
                <th>{t("dashboard.critical")}</th>
              </tr>
            </thead>
            <tbody>
              {data.low_stock_items.map((m) => (
                <tr key={m.id}>
                  <td>
                    <strong>{m.name}</strong>
                    {/* «Нет в наличии» и «на исходе» — разные состояния, а список
                        общий: без пометки владелец видит в «на исходе» позиции,
                        которых просто ещё не закупали. */}
                    <span className={`badge ${Number(m.quantity) > 0 ? "warn" : ""}`} style={{ marginLeft: 6 }}>
                      {Number(m.quantity) > 0 ? t("warehouse.lowStock") : t("checkout.outOfStock")}
                    </span>
                  </td>
                  <td style={{ color: Number(m.quantity) > 0 ? "var(--danger)" : "var(--ink-muted)", fontWeight: 600 }}>
                    {qtyFmt(m.quantity)} {t(`unit.${m.unit}`)}
                    {m.sheets_remaining != null ? ` · ≈${Math.round(Number(m.sheets_remaining))} ${t("warehouse.sheetsShort")}` : ""}
                  </td>
                  <td className="muted">{qtyFmt(m.critical_balance)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      )}

      {/* Who buys how much material */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("dashboard.clientMaterials")}</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("dashboard.clientMaterialsHint")}</p>
        {clientBuys.length ? (
          <div className="table-scroll">
          <table className="table plain-table">
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("clients.phone")}</th>
                <th>{t("dashboard.materialSpend")}</th>
                <th>{t("dashboard.materialQty")}</th>
                <th>{t("clients.orders")}</th>
              </tr>
            </thead>
            <tbody>
              {clientBuys.map((r) => (
                <tr key={r.client_id}>
                  <td><strong>{r.client_name}</strong></td>
                  <td className="muted">{r.phone}</td>
                  <td>{som(r.material_spend)}</td>
                  <td>{Number(r.material_qty)}</td>
                  <td>{r.orders}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        ) : (
          <p className="muted">{t("common.empty")}</p>
        )}
      </div>
    </>
  );
}
