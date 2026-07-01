import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const BAR_H = 90; // px above and below the zero baseline

// Day-by-day profit/loss bar chart for one calendar month, so the owner can
// see which days were in the red — not just the month-end total.
export default function DailyProfitChart() {
  const { t } = useTranslation();
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1); // 1-12
  const [data, setData] = useState(null);
  const [hoverDay, setHoverDay] = useState(null);

  useEffect(() => {
    setData(null);
    api.get("/finance/daily/", { params: { year, month } }).then((r) => setData(r.data));
  }, [year, month]);

  function shift(delta) {
    let m = month + delta;
    let y = year;
    if (m < 1) { m = 12; y -= 1; }
    if (m > 12) { m = 1; y += 1; }
    setYear(y);
    setMonth(m);
  }

  const isCurrentMonth = year === now.getFullYear() && month === now.getMonth() + 1;
  const rows = data?.rows || [];
  // Future days (profit === null — the day hasn't happened yet) don't get a
  // bar at all, so the chart simply stops at today instead of showing them
  // as pre-emptively "in the red" for rent they haven't had a chance to earn.
  const pastRows = rows.filter((r) => r.profit != null);
  const maxAbs = Math.max(1, ...pastRows.map((r) => Math.abs(Number(r.profit))));
  const hovered = hoverDay != null ? rows.find((r) => r.day === hoverDay) : null;

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>{t("finance.dailyTitle")}</h3>
        <div className="row" style={{ gap: 2, alignItems: "center" }}>
          <button className="ghost" onClick={() => shift(-1)} aria-label={t("common.prevMonth")}>
            ‹
          </button>
          <strong style={{ minWidth: 130, textAlign: "center" }}>
            {t(`finance.m${month}`)} {year}
          </strong>
          <button
            className="ghost"
            onClick={() => shift(1)}
            disabled={isCurrentMonth}
            aria-label={t("common.nextMonth")}
          >
            ›
          </button>
        </div>
      </div>

      {!data ? (
        <p className="muted">{t("common.loading")}</p>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${rows.length * 20} ${BAR_H * 2 + 4}`}
            style={{ width: "100%", height: 170, display: "block" }}
            preserveAspectRatio="none"
          >
            <line
              x1="0" y1={BAR_H + 2} x2={rows.length * 20} y2={BAR_H + 2}
              stroke="var(--canvas)" strokeWidth="2"
            />
            {rows.map((r, i) => {
              if (r.profit == null) return null; // future day — no bar yet
              const profit = Number(r.profit);
              const h = Math.max(1, (Math.abs(profit) / maxAbs) * BAR_H);
              const x = i * 20 + 3;
              const isToday = data.today === r.date;
              const y = profit >= 0 ? BAR_H + 2 - h : BAR_H + 2;
              return (
                <rect
                  key={r.date}
                  x={x} y={y} width={14} height={h}
                  rx="2"
                  fill={profit >= 0 ? "var(--ok)" : "var(--danger)"}
                  opacity={hoverDay == null || hoverDay === r.day ? 1 : 0.45}
                  stroke={isToday ? "var(--accent-strong)" : "none"}
                  strokeWidth={isToday ? 2 : 0}
                  onMouseEnter={() => setHoverDay(r.day)}
                  onMouseLeave={() => setHoverDay(null)}
                >
                  <title>
                    {r.day} {t(`finance.m${month}`)}: {profit >= 0 ? "+" : ""}
                    {som(profit)}
                  </title>
                </rect>
              );
            })}
          </svg>

          <p className="muted" style={{ textAlign: "center", minHeight: 20, marginTop: 4 }}>
            {hovered ? (
              <>
                {hovered.day} {t(`finance.m${month}`)}:{" "}
                <strong style={{ color: Number(hovered.profit) >= 0 ? "var(--ok)" : "var(--danger)" }}>
                  {Number(hovered.profit) >= 0 ? "+" : ""}
                  {som(hovered.profit)}
                </strong>
              </>
            ) : (
              t("finance.dailyHoverHint")
            )}
          </p>

          <div className="stat-grid" style={{ marginTop: 8 }}>
            <div className="stat">
              <div className="label">{t("finance.revenue")}</div>
              <div className="value">{som(data.totals.revenue)}</div>
            </div>
            <div className="stat">
              <div className="label">{t("finance.expenses")}</div>
              <div className="value">
                {som(Number(data.totals.variable) + Number(data.totals.fixed))}
              </div>
            </div>
            <div className="stat">
              <div className="label">{t("finance.profit")}</div>
              <div
                className="value"
                style={{ color: Number(data.totals.profit) >= 0 ? "var(--ok)" : "var(--danger)" }}
              >
                {som(data.totals.profit)}
              </div>
            </div>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            {t("finance.dailyHint")}
          </p>
        </>
      )}
    </div>
  );
}
