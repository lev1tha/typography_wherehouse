import { useTranslation } from "react-i18next";

import Hint from "./Hint.jsx";
import { formatDate, formatMoney, formatNumber } from "../utils/format.js";

const som = (n) => formatMoney(n);
const q2 = (n) => formatNumber(n, { max: 2 });
const num = (v) => Number(v) || 0;
const dash = <span className="muted">—</span>;
const cell = (v) => (num(v) ? som(v) : dash);

// «Резка по станкам» целиком (STAFF-03/-04): по каждому станку резка и «прочие
// работы» (гравировка, монтаж…) отдельными колонками; возврат — своей колонкой,
// а не вычитанием строки из отчёта (станок, на котором всё вернули, остаётся
// строкой); ниже — ряд по дням. Отходы — не работа станка и сюда не входят.
export function MachineTable({ cutting }) {
  const { t } = useTranslation();
  const rows = cutting?.rows || [];
  const days = cutting?.days || [];
  if (!rows.length && !days.length) return null;
  const keyOf = (r) => r.id ?? "";
  const machineCols = rows.filter((r) => num(r.sold) || num(r.amount) || num(r.returned));
  const sum = (f) => rows.reduce((s, r) => s + num(r[f]), 0);

  return (
    <>
      <h4 style={{ margin: "18px 0 2px" }}>{t("workReport.machinesTitle")}</h4>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("workReport.machinesHint")}</p>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{t("workReport.machine")}</th>
              <th scope="col">{t("workReport.sold")}</th>
              <th scope="col">{t("workReport.returned")}<Hint text={t("workReport.returnedHint")} /></th>
              <th scope="col">{t("workReport.net")}</th>
              <th scope="col">{t("workReport.otherWorks")}<Hint text={t("workReport.otherWorksHint")} /></th>
              <th scope="col">{t("finance.sqmShort")}</th>
              <th scope="col">{t("payroll.pmShort")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={keyOf(r) || "none"}>
                <td><strong>{r.name}</strong></td>
                <td>{cell(r.sold)}</td>
                <td>{num(r.returned) ? <span style={{ color: "var(--danger-ink)" }}>− {som(r.returned)}</span> : dash}</td>
                <td><strong>{som(r.amount)}</strong></td>
                <td>
                  {cell(r.other_amount)}
                  {num(r.other_returned) > 0 && (
                    <div className="muted" style={{ fontSize: 12 }}>{t("workReport.otherReturned", { sum: som(r.other_returned) })}</div>
                  )}
                </td>
                <td>{q2(r.area)}</td>
                <td>{q2(r.running_meters)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="sheet-total">
              <td><strong>{t("common.total")}</strong></td>
              <td><strong>{som(sum("sold"))}</strong></td>
              <td><strong>{num(sum("returned")) ? `− ${som(sum("returned"))}` : "—"}</strong></td>
              <td><strong>{som(cutting.total)}</strong></td>
              <td><strong>{som(cutting.other_total)}</strong></td>
              <td><strong>{q2(cutting.area)}</strong></td>
              <td><strong>{q2(cutting.running_meters)}</strong></td>
            </tr>
          </tfoot>
        </table>
      </div>

      {days.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary style={{ cursor: "pointer", fontWeight: 600, color: "var(--accent-ink)" }}>
            {t("workReport.byDay")}
          </summary>
          <div className="table-wrap" style={{ marginTop: 8 }}>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("cash.date")}</th>
                  {machineCols.map((m) => <th scope="col" key={keyOf(m) || "none"}>{m.name}</th>)}
                  <th scope="col">{t("workReport.otherWorks")}</th>
                  <th scope="col">{t("workReport.returned")}</th>
                  <th scope="col">{t("workReport.dayTotal")}</th>
                </tr>
              </thead>
              <tbody>
                {days.map((d) => (
                  <tr key={d.date}>
                    <td>{formatDate(d.date)}</td>
                    {machineCols.map((m) => <td key={keyOf(m) || "none"}>{cell(d.machines?.[keyOf(m)])}</td>)}
                    <td>{cell(d.other)}</td>
                    <td>{num(d.returned) ? <span style={{ color: "var(--danger-ink)" }}>− {som(d.returned)}</span> : dash}</td>
                    <td><strong>{som(d.total)}</strong></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </>
  );
}

// Выручка и маржа по видам услуг (PNL-06): резка, гравировка, установка, буквы,
// отходы, прочее — и каждая услуга внутри вида отдельно (прямой и кривой рез,
// если это разные услуги). Маржа = выручка − расходники по техкарте; доля
// мастера вычитается только если включена настройка «учитывать долю мастера в
// марже» (в ОПиУ она не влияет: там зарплата уже расходом).
export function ServiceMargin({ services }) {
  const { t } = useTranslation();
  if (!services || (!services.rows.length && !num(services.materials?.revenue))) return null;
  const showShare = services.master_share_included;
  const marginColor = (v) => (num(v) < 0 ? "var(--danger-ink)" : undefined);
  const head = (
    <tr>
      <th scope="col">{t("workReport.kind")}</th>
      <th scope="col">{t("workReport.revenue")}</th>
      <th scope="col">{t("workReport.cost")}</th>
      {showShare && <th scope="col">{t("workReport.masterShare")}</th>}
      <th scope="col">{t("workReport.margin")}</th>
    </tr>
  );

  return (
    <>
      <h4 style={{ margin: "18px 0 2px" }}>
        {t("workReport.servicesTitle")}
        <Hint text={t("workReport.servicesHint")} />
      </h4>
      <div className="table-wrap">
        <table className="table">
          <thead>{head}</thead>
          <tbody>
            <tr>
              <td><strong>{t("workReport.materials")}</strong></td>
              <td>{som(services.materials.revenue)}</td>
              <td>{som(services.materials.cost)}</td>
              {showShare && <td>{dash}</td>}
              <td style={{ color: marginColor(services.materials.margin) }}><strong>{som(services.materials.margin)}</strong></td>
            </tr>
            {services.rows.map((g) => (
              <ServiceGroup key={g.key} g={g} showShare={showShare} t={t} marginColor={marginColor} />
            ))}
            {/* Гарантийные переделки (волна 2): выручки нет, себестоимость —
                своей строкой, как в ОПиУ. */}
            {num(services.warranty?.cost) !== 0 && (
              <tr>
                <td><strong>{t("ladder.warranty")}</strong></td>
                <td>{som(0)}</td>
                <td>{som(services.warranty.cost)}</td>
                {showShare && <td>{dash}</td>}
                <td style={{ color: "var(--danger-ink)" }}><strong>{som(-num(services.warranty.cost))}</strong></td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {Math.abs(num(services.unallocated)) >= 1 && (
        <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
          {t("workReport.unallocated", { sum: som(services.unallocated) })}
        </p>
      )}
      <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
        {showShare ? t("workReport.shareOn") : t("workReport.shareOff")}
      </p>
    </>
  );
}

function ServiceGroup({ g, showShare, t, marginColor }) {
  return (
    <>
      <tr>
        <td><strong>{t(`workReport.group_${g.key}`)}</strong></td>
        <td>{som(g.revenue)}</td>
        <td>{num(g.cost) ? som(g.cost) : dash}</td>
        {showShare && <td>{num(g.master_share) ? som(g.master_share) : dash}</td>}
        <td style={{ color: marginColor(g.margin) }}><strong>{som(g.margin)}</strong></td>
      </tr>
      {g.services.length > 1 &&
        g.services.map((s) => (
          <tr key={s.id} className="muted">
            <td style={{ paddingLeft: 28 }}>{s.name}</td>
            <td>{som(s.revenue)}</td>
            <td>{num(s.cost) ? som(s.cost) : dash}</td>
            {showShare && <td>{num(s.master_share) ? som(s.master_share) : dash}</td>}
            <td>{som(s.margin)}</td>
          </tr>
        ))}
    </>
  );
}
