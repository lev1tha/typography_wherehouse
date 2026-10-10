import { Fragment, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import Hint from "../../components/Hint.jsx";
import Icon from "../../components/Icon.jsx";
import LoadError from "../../components/LoadError.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import PayrollAdjustModal from "../../components/PayrollAdjustModal.jsx";
import PayrollPayModal from "../../components/PayrollPayModal.jsx";
import PayrollRulesModal from "../../components/PayrollRulesModal.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { downloadFile } from "../../utils/download.js";
import { formatDate, formatMoney, formatNumber } from "../../utils/format.js";

// «Ведомость за месяц» (аудит STAFF-01/-09/-10, cash-11): по каждому человеку —
// начислено, удержано, аванс, выплачено и «к выдаче». Заменяет лист «ЗП октябрь»:
// оклад, проценты по видам работ, премия за выработку считаются по правилам,
// которые владелец задаёт сам; выработка — по заказам месяца.
//
// Как это ложится в отчёты:
//   · «Начислить» проводит месяц: зарплата попадает в ОПиУ месяца расходом
//     «начислено, не выплачено» — денег в кассе при этом не движется;
//   · «Выплатить» и «Аванс» пишут расход в кассовую книгу и гасят начисление;
//     в ОПиУ они не идут (зарплата там уже начислена).

const som = (n) => formatMoney(n, { fraction: 0 });
const pad = (n) => String(n).padStart(2, "0");
const ym = (p) => `${p.year}-${pad(p.month)}`;

function Tile({ label, value, hint, color }) {
  return (
    <div className="stat">
      <div className="label">{label}{hint ? <Hint text={hint} /> : null}</div>
      <div className="value" style={color ? { color: `var(--${color}-ink)` } : undefined}>{value}</div>
    </div>
  );
}

export default function Payroll() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAccountant: readOnly } = useAuth();
  const now = new Date();
  const [period, setPeriod] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 });
  const month = period.month ? ym(period) : ym({ year: now.getFullYear(), month: now.getMonth() + 1 });
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(() => new Set());
  const [paying, setPaying] = useState(null);       // {employee|null}
  const [adjusting, setAdjusting] = useState(null);
  const [rules, setRules] = useState(null);
  const [busy, setBusy] = useState(false);
  const wanted = useRef("");

  function load() {
    wanted.current = month;
    api.get("/finance/payroll/", { params: { month } })
      .then((r) => { if (wanted.current === month) { setData(r.data); setFailed(false); } })
      .catch((e) => { setFailed(true); toast(apiError(e, t("common.loadFailed")), "error"); });
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setData(null); load(); }, [month]);

  const toggle = (id) => setOpen((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  async function accrue() {
    setBusy(true);
    try {
      const r = await api.post("/finance/payroll/accrue/", { month });
      setData(r.data);
      toast(t("payroll.accrued"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function unpost() {
    if (!(await confirm(t("payroll.confirmUnpost")))) return;
    try {
      const r = await api.delete("/finance/payroll/accrue/", { params: { month } });
      setData(r.data);
      toast(t("payroll.unposted"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function removeItem(url, text) {
    if (!(await confirm(text))) return;
    try {
      await api.delete(url);
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  if (failed && !data) return <LoadError onRetry={load} />;

  const rows = data?.rows || [];
  const totals = data?.totals;
  const anyUnposted = rows.some((r) => !r.posted && (Number(r.gross) > 0 || Number(r.deductions) > 0));
  const anyPosted = rows.some((r) => r.posted);
  const anyStale = rows.some((r) => r.stale);

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 12 }}>
        <div>
          <h1 style={{ margin: 0 }}>{t("payroll.title")}</h1>
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 13, maxWidth: "70ch" }}>{t("payroll.hint")}</p>
        </div>
        <MonthPicker value={{ year: period.year, month: period.month || now.getMonth() + 1 }} onChange={setPeriod} />
      </div>

      {!data ? (
        <p className="muted">{t("common.loading")}</p>
      ) : (
        <>
          <div className="stat-grid" style={{ marginTop: 14 }}>
            <Tile label={t("payroll.colAccrued")} value={som(totals.accrued)} hint={t("payroll.tipAccrued")} />
            <Tile label={t("payroll.colDeductions")} value={som(totals.deductions)} hint={t("payroll.tipDeductions")} />
            <Tile label={t("payroll.colAdvance")} value={som(totals.advances)} />
            <Tile label={t("payroll.colPaid")} value={som(totals.payouts)} />
            <Tile
              label={t("payroll.colDue")}
              value={som(totals.to_pay)}
              hint={t("payroll.tipDue")}
              color={Number(totals.to_pay) > 0 ? "accent" : Number(totals.to_pay) < 0 ? "danger" : "ok"}
            />
          </div>

          {anyUnposted && (
            <div className="callout" role="status" style={{ marginTop: 12 }}>
              {t("payroll.notPosted")}
            </div>
          )}
          {anyStale && (
            <div className="callout" role="status" style={{ marginTop: 12 }}>
              {t("payroll.stale")}
            </div>
          )}

          <div className="row" style={{ marginTop: 12, gap: 8, flexWrap: "wrap" }}>
            {!readOnly && (
              <>
                <button onClick={accrue} disabled={busy || !rows.length}>
                  {anyPosted ? t("payroll.reaccrue") : t("payroll.accrue")}
                </button>
                <button className="secondary" onClick={() => setPaying({ employee: null })} disabled={!rows.length}>
                  {t("payroll.newPayment")}
                </button>
                {anyPosted && <button className="secondary" onClick={unpost}>{t("payroll.unpost")}</button>}
              </>
            )}
            <button className="secondary" onClick={() => downloadFile("/finance/payroll/export/", { month }, `vedomost-${month}.csv`)
              .catch((e) => toast(apiError(e, t("common.error")), "error"))}>
              {t("statements.csv")}
            </button>
            {!readOnly && (
              <Link to="/admin/staff" className="btn-link">{t("payroll.toStaff")} <Icon name="arrow-right" size={16} /></Link>
            )}
          </div>

          {rows.length === 0 ? (
            <div className="empty-state" style={{ marginTop: 16 }}>
              <Icon name="users" size={40} className="es-icon" />
              {t("payroll.empty")}
            </div>
          ) : (
            <div className="table-wrap" style={{ marginTop: 14 }}>
              <table className="table payroll-table">
                <thead>
                  <tr>
                    <th scope="col">{t("salary.employee")}</th>
                    <th scope="col">{t("payroll.colCalc")}<Hint text={t("payroll.tipCalc")} /></th>
                    <th scope="col">{t("payroll.colDeductions")}</th>
                    <th scope="col">{t("payroll.colInPnl")}<Hint text={t("payroll.tipInPnl")} /></th>
                    <th scope="col">{t("payroll.colAdvance")}</th>
                    <th scope="col">{t("payroll.colPaid")}</th>
                    <th scope="col">{t("payroll.colDue")}</th>
                    {!readOnly && <th scope="col" />}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const id = r.employee.id;
                    const isOpen = open.has(id);
                    return (
                      <Fragment key={id}>
                        <tr className={r.employee.is_active ? "" : "row-muted"}>
                          <td>
                            <button className="ghost" style={{ padding: 0, height: "auto", textAlign: "left" }}
                              aria-expanded={isOpen} onClick={() => toggle(id)}>
                              <Icon name="chevron-right" size={14}
                                style={{ transform: isOpen ? "rotate(90deg)" : undefined, transition: "transform .15s" }} />{" "}
                              <strong>{r.employee.name}</strong>
                            </button>
                            <div className="muted" style={{ fontSize: 12 }}>
                              {[r.employee.position, r.employee.machine ? t(`staff.machine_${r.employee.machine}`) : ""]
                                .filter(Boolean).join(" · ")}
                              {!r.scheme && <span className="badge amber" style={{ marginLeft: 6 }}>{t("payroll.noRules")}</span>}
                            </div>
                          </td>
                          <td>{som(r.gross)}</td>
                          <td>{Number(r.deductions) > 0 ? <span style={{ color: "var(--danger-ink)" }}>− {som(r.deductions)}</span> : <span className="muted">—</span>}</td>
                          <td>
                            {som(r.accrued)}
                            {r.posted
                              ? r.stale
                                ? <div><span className="badge amber">{t("payroll.badgeStale")}</span></div>
                                : <div><span className="badge ok">{t("payroll.badgePosted")}</span></div>
                              : Number(r.gross) > 0 || Number(r.deductions) > 0
                                ? <div><span className="badge">{t("payroll.badgeNotPosted")}</span></div>
                                : null}
                          </td>
                          <td>{Number(r.advances) > 0 ? som(r.advances) : <span className="muted">—</span>}</td>
                          <td>{Number(r.payouts) > 0 ? som(r.payouts) : <span className="muted">—</span>}</td>
                          <td>
                            <strong style={{ color: Number(r.to_pay) < 0 ? "var(--danger-ink)" : Number(r.to_pay) > 0 ? "var(--accent-ink)" : undefined }}>
                              {som(r.to_pay)}
                            </strong>
                          </td>
                          {!readOnly && (
                            <td>
                              <span className="row" style={{ gap: 4, margin: 0, flexWrap: "wrap" }}>
                                <button className="ghost row-btn" onClick={() => setPaying({ employee: r })}>{t("payroll.pay")}</button>
                                <button className="ghost row-btn" onClick={() => setAdjusting(r)}>{t("payroll.deduct")}</button>
                                <button className="ghost row-btn" onClick={() => setRules(r)}>{t("payroll.rules")}</button>
                              </span>
                            </td>
                          )}
                        </tr>
                        {isOpen && (
                          <tr>
                            <td colSpan={readOnly ? 7 : 8} style={{ background: "var(--canvas)" }}>
                              <Breakdown r={r} t={t} readOnly={readOnly} onRules={() => setRules(r)}
                                onRemovePayment={(p) => removeItem(`/finance/payroll/payments/${p.id}/`, t("payroll.confirmDelPayment"))}
                                onRemoveAdjustment={(a) => removeItem(`/finance/payroll/adjustments/${a.id}/`, t("payroll.confirmDelAdjustment"))} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
                <tfoot>
                  <tr className="sheet-total">
                    <td><strong>{t("common.total")}</strong></td>
                    <td><strong>{som(totals.gross)}</strong></td>
                    <td><strong>{som(totals.deductions)}</strong></td>
                    <td><strong>{som(totals.accrued)}</strong></td>
                    <td><strong>{som(totals.advances)}</strong></td>
                    <td><strong>{som(totals.payouts)}</strong></td>
                    <td><strong>{som(totals.to_pay)}</strong></td>
                    {!readOnly && <td />}
                  </tr>
                </tfoot>
              </table>
            </div>
          )}

          {data.unassigned.length > 0 && (
            <div className="card" style={{ marginTop: 14 }}>
              <h4 style={{ margin: "0 0 4px" }}>{t("payroll.unassignedTitle")}</h4>
              <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("payroll.unassignedHint")}</p>
              {data.unassigned.map((u) => (
                <div className="crow" key={u.work}>
                  <span className="k">{t(`payroll.work_${u.work}`)}</span>
                  <span>{som(u.base)}</span>
                </div>
              ))}
            </div>
          )}
          <p className="muted" style={{ fontSize: 12, marginTop: 12, maxWidth: "80ch" }}>{t("payroll.footnote")}</p>
        </>
      )}

      {paying && (
        <PayrollPayModal
          employee={paying.employee}
          employees={rows.map((r) => ({ id: r.employee.id, name: r.employee.name, is_active: r.employee.is_active }))}
          month={month}
          onClose={() => setPaying(null)}
          onDone={() => { setPaying(null); load(); }}
        />
      )}
      {adjusting && (
        <PayrollAdjustModal
          employee={adjusting.employee}
          month={month}
          onClose={() => setAdjusting(null)}
          onDone={() => { setAdjusting(null); load(); }}
        />
      )}
      {rules && (
        <PayrollRulesModal
          employee={rules.employee}
          month={month}
          readOnly={readOnly}
          onClose={() => setRules(null)}
          onChanged={load}
        />
      )}
    </>
  );
}

// «Из чего сложилось»: оклад, проценты по видам работ, премия; удержания и
// выплаты месяца с возможностью убрать запись.
function Breakdown({ r, t, readOnly, onRules, onRemovePayment, onRemoveAdjustment }) {
  const bonus = r.bonus;
  return (
    <div style={{ padding: "6px 4px", display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
      <div>
        <div className="mat-sub">{t("payroll.calcTitle")}</div>
        <div className="crow"><span className="k">{t("payroll.salary")}</span><span>{som(r.salary)}</span></div>
        {r.lines.map((l) => (
          <div className="crow" key={l.work}>
            <span className="k">
              {t(`payroll.work_${l.work}`)}
              <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
                {t("payroll.lineBase", { base: som(l.base), percent: formatNumber(l.percent, { max: 2 }) })}
                {Number(l.meters) > 0 && ` · ${formatNumber(l.meters, { max: 2 })} ${t("payroll.pmShort")}`}
              </div>
            </span>
            <span>{som(l.amount)}</span>
          </div>
        ))}
        {bonus.threshold != null && (
          <div className="crow">
            <span className="k">
              {t("payroll.bonus")}
              <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
                {t("payroll.bonusState", {
                  value: formatNumber(bonus.value, { max: 2 }),
                  threshold: formatNumber(bonus.threshold, { max: 2 }),
                  metric: t(`payroll.metricShort_${bonus.metric}`),
                })}
              </div>
            </span>
            <span>{bonus.earned ? som(bonus.amount) : <span className="muted">{t("payroll.bonusNo")}</span>}</span>
          </div>
        )}
        <div className="crow mat-result">
          <strong>{t("payroll.colCalc")}</strong><strong>{som(r.gross)}</strong>
        </div>
        {r.scheme_from && (
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
            {t("payroll.rulesFromNote", { month: `${r.scheme_from.slice(5, 7)}.${r.scheme_from.slice(0, 4)}` })}
          </p>
        )}
        {!r.scheme && !readOnly && (
          <button className="ghost" style={{ color: "var(--accent-ink)", fontWeight: 600 }} onClick={onRules}>
            + {t("payroll.rulesAdd")}
          </button>
        )}
      </div>
      <div>
        <div className="mat-sub">{t("payroll.deductionsTitle")}</div>
        {r.adjustments.length === 0 && <p className="muted" style={{ fontSize: 13 }}>{t("payroll.none")}</p>}
        {r.adjustments.map((a) => (
          <div className="crow" key={a.id}>
            <span className="k">
              {t(`payroll.reason_${a.reason}`)}
              {a.note && <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{a.note}</div>}
              {a.inventory_log && <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{t("payroll.writeoffNo", { n: a.inventory_log })}</div>}
            </span>
            <span className="row" style={{ gap: 4, margin: 0, alignItems: "center" }}>
              <span style={{ color: "var(--danger-ink)" }}>− {som(a.amount)}</span>
              {!readOnly && (
                <button className="ghost" onClick={() => onRemoveAdjustment(a)} aria-label={t("common.delete")}>
                  <Icon name="trash" size={14} />
                </button>
              )}
            </span>
          </div>
        ))}
      </div>
      <div>
        <div className="mat-sub">{t("payroll.paymentsTitle")}</div>
        {r.payments.length === 0 && <p className="muted" style={{ fontSize: 13 }}>{t("payroll.none")}</p>}
        {r.payments.map((p) => (
          <div className="crow" key={p.id}>
            <span className="k">
              {t(p.kind === "ADVANCE" ? "payroll.advance" : "payroll.payout")} · {formatDate(p.paid_on)}
              <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
                {p.account === "BANK" ? t("expenses.paidBank") : t("expenses.paidCash")}{p.note ? ` · ${p.note}` : ""}
              </div>
            </span>
            <span className="row" style={{ gap: 4, margin: 0, alignItems: "center" }}>
              <span>{som(p.amount)}</span>
              {!readOnly && (
                <button className="ghost" onClick={() => onRemovePayment(p)} aria-label={t("common.delete")}>
                  <Icon name="trash" size={14} />
                </button>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
