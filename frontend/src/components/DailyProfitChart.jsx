import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const num = (n) => Math.round(Number(n) || 0).toLocaleString("ru-RU");

// Прибыль по дням месяца — чтобы владелец видел, какие дни ушли в минус, а не
// только итог за месяц.
//
// График переписан 2026-08-14: заказчик сказал «непонятно, как показывает».
// Было три беды сразу. Под убытки всегда резервировалась ровно половина
// высоты — в месяце без убытков нижняя половина карточки стояла пустой, а все
// столбики жались к верху. Дни не были подписаны: какой столбик какое число,
// можно было узнать только наведением мышью (о чём и просила подпись под
// графиком). И не было масштаба — по столбику нельзя понять, там пять тысяч
// или пятьсот. Теперь нулевая линия стоит там, где ей место (внизу, если
// убытков нет), под столбиками стоят числа, а слева — шкала.
export default function DailyProfitChart({ year: propYear, month: propMonth, reloadKey = 0 }) {
  const { t } = useTranslation();
  const now = new Date();
  const [year, setYear] = useState(propYear ?? now.getFullYear());
  const [month, setMonth] = useState(propMonth ?? now.getMonth() + 1); // 1-12
  const [data, setData] = useState(null);
  const [hoverDay, setHoverDay] = useState(null);

  // График всегда про один месяц, но период страницы может стоять на другом
  // (или на «весь период» — тогда месяц пропадает и остаётся выбранный здесь).
  // Идём за страницей, а стрелки продолжают листать месяцы локально.
  useEffect(() => {
    if (propYear) setYear(propYear);
    if (propMonth) setMonth(propMonth);
  }, [propYear, propMonth]);

  // reloadKey растёт, когда со страницы поменяли траты, — иначе график остался
  // бы на старых цифрах и спорил бы со сводкой над ним.
  useEffect(() => {
    setData(null);
    api.get("/finance/daily/", { params: { year, month } }).then((r) => setData(r.data));
  }, [year, month, reloadKey]);

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
  // Будущие дни (profit === null — день ещё не наступил) столбика не получают:
  // иначе они стояли бы в минусе на аренду, которую ещё не было шанса отбить.
  // Колонку под них оставляем пустой, чтобы месяц не «заканчивался» 14-м числом.
  const pastRows = rows.filter((r) => r.profit != null);
  const values = pastRows.map((r) => Number(r.profit));
  const maxPos = Math.max(0, ...values);
  const maxNeg = Math.min(0, ...values);
  // Высоту делим между плюсом и минусом по их РЕАЛЬНОМУ размаху: нет убытков —
  // нулевая линия внизу и весь график про прибыль.
  const span = maxPos - maxNeg;
  const zeroPct = span > 0 ? (maxPos / span) * 100 : 100;
  const hovered = hoverDay != null ? rows.find((r) => r.day === hoverDay) : null;
  // В дате месяц идёт в родительном падеже: «15 июля», а не «15 Июль».
  const monthOf = t("monthsOf", { returnObjects: true })[month - 1] || t(`finance.m${month}`);

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
      {/* Что именно на графике — прямо под заголовком, а не мелким текстом
          в самом низу карточки, где это никто не читает. */}
      <p className="muted" style={{ fontSize: 13, margin: "2px 0 14px" }}>
        {t("finance.dailySubtitle")}
      </p>

      {!data ? (
        <p className="muted">{t("common.loading")}</p>
      ) : (
        <>
          <div className="dc">
            {/* Шкала: сколько стоит самый высокий столбик. Без неё непонятно,
                пять там тысяч или пятьсот. */}
            <div className="dc-axis">
              <span className="dc-tick" style={{ top: 0 }}>{num(maxPos)}</span>
              <span className="dc-tick" style={{ top: `${zeroPct}%` }}>0</span>
              {maxNeg < 0 && (
                <span className="dc-tick dc-tick-neg" style={{ top: "100%" }}>{num(maxNeg)}</span>
              )}
            </div>

            <div className="dc-chart">
              {rows.map((r) => {
                const profit = r.profit == null ? null : Number(r.profit);
                const isToday = data.today === r.date;
                const height = profit == null || span <= 0 ? 0 : (Math.abs(profit) / span) * 100;
                return (
                  <div
                    className="dc-col"
                    key={r.date}
                    onMouseEnter={() => setHoverDay(r.day)}
                    onMouseLeave={() => setHoverDay(null)}
                    title={
                      profit == null
                        ? undefined
                        : `${r.day} ${monthOf}: ${profit >= 0 ? "+" : ""}${som(profit)}`
                    }
                  >
                    <div className="dc-plot" style={{ "--zero": `${zeroPct}%` }}>
                      {profit != null && (
                        <div
                          /* День без продаж и трат — серый огрызок у нуля, не
                             зелёный: ноль это не «в плюсе». Ряд зелёных чёрточек
                             в начале месяца читался как «работали и заработали
                             ничего». */
                          className={`dc-bar ${profit === 0 ? "zero" : profit > 0 ? "pos" : "neg"}`}
                          style={
                            profit >= 0
                              ? { bottom: `${100 - zeroPct}%`, height: `${height}%` }
                              : { top: `${zeroPct}%`, height: `${height}%` }
                          }
                          data-dim={hoverDay != null && hoverDay !== r.day ? "1" : undefined}
                        />
                      )}
                    </div>
                    <div className={`dc-day${isToday ? " today" : ""}${r.day % 5 === 0 || r.day === 1 ? " keep" : ""}`}>
                      {r.day}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <p className="muted" style={{ textAlign: "center", minHeight: 20, marginTop: 6 }}>
            {hovered && hovered.profit != null ? (
              <>
                {hovered.day} {monthOf}:{" "}
                <strong style={{ color: Number(hovered.profit) >= 0 ? "var(--ok)" : "var(--danger)" }}>
                  {Number(hovered.profit) >= 0 ? "+" : ""}
                  {som(hovered.profit)}
                </strong>
                {" · "}
                <span style={{ fontSize: 13 }}>
                  {t("finance.revenue")} {som(hovered.revenue)}
                </span>
              </>
            ) : (
              t("finance.dailyHoverHint")
            )}
          </p>

          {/* Итоги месяца — строкой, а не рядом плиток: карточка в карточке
              выглядела вторым, спорящим с верхом страницы блоком. */}
          <div className="dc-totals">
            <span>
              <span className="k">{t("finance.revenue")}</span>
              <strong>{som(data.totals.revenue)}</strong>
            </span>
            <span>
              <span className="k">{t("finance.expenses")}</span>
              <strong>{som(Number(data.totals.variable) + Number(data.totals.fixed))}</strong>
            </span>
            <span>
              <span className="k">{t("finance.profit")}</span>
              <strong style={{ color: Number(data.totals.profit) >= 0 ? "var(--ok)" : "var(--danger)" }}>
                {som(data.totals.profit)}
              </strong>
            </span>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            {t("finance.dailyHint")}
          </p>
        </>
      )}
    </div>
  );
}
