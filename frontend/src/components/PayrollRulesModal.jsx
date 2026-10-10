import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney, formatNumber } from "../utils/format.js";

const WORKS = ["CUTTING", "CUTTING_CNC", "CUTTING_LASER", "ENGRAVING", "INSTALL", "OTHER"];

const monthLabel = (ym) => (ym ? `${ym.slice(5, 7)}.${ym.slice(0, 4)}` : "");

// Правила оплаты сотрудника с историей (STAFF-05): оклад, процент от работ по
// видам и станкам, премия за выработку выше порога. Новое правило действует с
// месяца, который вы укажете; прошлые месяцы считаются по прежним.
export default function PayrollRulesModal({ employee, month, readOnly, onClose, onChanged }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [schemes, setSchemes] = useState(null);
  const [form, setForm] = useState(null);   // {id?, valid_from, salary, ..., rates: [{work, percent}]}
  const [error, setError] = useState("");

  function load() {
    api.get("/finance/pay-schemes/", { params: { employee: employee.id } })
      .then((r) => setSchemes(r.data.results || r.data))
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [employee.id]);

  function blank() {
    const last = schemes?.[0];
    return {
      valid_from: month,
      salary: last ? String(Number(last.salary)) : "",
      bonus_metric: last?.bonus_metric || "RUNNING_METERS",
      bonus_threshold: last?.bonus_threshold != null ? String(Number(last.bonus_threshold)) : "",
      bonus_amount: last ? String(Number(last.bonus_amount)) : "",
      note: "",
      rates: (last?.rates || []).map((r) => ({ work: r.work, percent: String(Number(r.percent)) })),
    };
  }

  function edit(s) {
    setError("");
    setForm({
      id: s.id, valid_from: s.valid_from, salary: String(Number(s.salary)), bonus_metric: s.bonus_metric,
      bonus_threshold: s.bonus_threshold != null ? String(Number(s.bonus_threshold)) : "",
      bonus_amount: String(Number(s.bonus_amount)), note: s.note || "",
      rates: s.rates.map((r) => ({ work: r.work, percent: String(Number(r.percent)) })),
    });
  }

  async function save() {
    for (const r of form.rates) {
      const p = Number(r.percent);
      if (r.percent === "" || !(p >= 0 && p <= 100)) return setError(t("payroll.percentBad"));
    }
    if (new Set(form.rates.map((r) => r.work)).size !== form.rates.length) return setError(t("payroll.workTwice"));
    setError("");
    const body = {
      employee: employee.id, valid_from: form.valid_from, salary: form.salary === "" ? 0 : form.salary,
      bonus_metric: form.bonus_metric,
      bonus_threshold: form.bonus_threshold === "" ? null : form.bonus_threshold,
      bonus_amount: form.bonus_amount === "" ? 0 : form.bonus_amount,
      note: form.note, rates: form.rates.map((r) => ({ work: r.work, percent: r.percent })),
    };
    try {
      if (form.id) await api.patch(`/finance/pay-schemes/${form.id}/`, body);
      else await api.post("/finance/pay-schemes/", body);
      setForm(null);
      load();
      onChanged?.();
      toast(t("common.saved"));
    } catch (e) {
      setError(apiError(e, t("common.error")));
    }
  }

  async function remove(s) {
    if (!(await confirm(t("payroll.confirmRulesDelete", { month: monthLabel(s.valid_from) })))) return;
    try {
      await api.delete(`/finance/pay-schemes/${s.id}/`);
      load();
      onChanged?.();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const setRate = (i, patch) => setForm({ ...form, rates: form.rates.map((r, k) => (k === i ? { ...r, ...patch } : r)) });

  return (
    <Modal
      wide
      title={t("payroll.rulesTitle", { name: employee.name })}
      onClose={onClose}
      footer={<button onClick={onClose}>{t("common.close")}</button>}
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("payroll.rulesHint")}</p>

      {!form && !readOnly && (
        <div className="row" style={{ margin: "6px 0 12px" }}>
          <button onClick={() => { setError(""); setForm(blank()); }}>+ {t("payroll.rulesAdd")}</button>
        </div>
      )}

      {form && (
        <div className="card" style={{ background: "var(--primary-soft)", margin: "6px 0 12px" }}>
          <div className="row">
            <Field style={{ width: 170 }} label={t("payroll.rulesFrom")} hint={t("payroll.rulesFromHint")}>
              <input type="month" value={form.valid_from} disabled={!!form.id}
                onChange={(e) => setForm({ ...form, valid_from: e.target.value })} />
            </Field>
            <Field style={{ width: 170 }} label={t("payroll.salary")}>
              <input type="number" min="0" step="any" value={form.salary} placeholder="0"
                onChange={(e) => setForm({ ...form, salary: e.target.value })} />
            </Field>
          </div>

          <h4 style={{ margin: "6px 0 2px" }}>{t("payroll.percents")}</h4>
          <p className="muted" style={{ fontSize: 12, margin: "0 0 6px" }}>{t("payroll.percentsHint")}</p>
          {form.rates.map((r, i) => (
            <div className="row" key={i} style={{ alignItems: "flex-end", gap: 8, margin: "0 0 4px" }}>
              <Field style={{ margin: 0, minWidth: 240 }} label={i === 0 ? t("payroll.work") : undefined}>
                <select value={r.work} onChange={(e) => setRate(i, { work: e.target.value })}>
                  {WORKS.map((w) => <option key={w} value={w}>{t(`payroll.work_${w}`)}</option>)}
                </select>
              </Field>
              <Field style={{ margin: 0, width: 120 }} label={i === 0 ? t("payroll.percent") : undefined}>
                <input type="number" min="0" max="100" step="0.01" value={r.percent}
                  onChange={(e) => setRate(i, { percent: e.target.value })} />
              </Field>
              <button className="ghost" aria-label={t("common.delete")}
                onClick={() => setForm({ ...form, rates: form.rates.filter((_, k) => k !== i) })}>
                <Icon name="trash" size={16} />
              </button>
            </div>
          ))}
          <button className="ghost" style={{ color: "var(--accent-ink)", fontWeight: 600 }}
            onClick={() => setForm({
              ...form,
              rates: [...form.rates, { work: WORKS.find((w) => !form.rates.some((r) => r.work === w)) || "CUTTING", percent: "" }],
            })}>
            + {t("payroll.addPercent")}
          </button>

          <h4 style={{ margin: "12px 0 2px" }}>{t("payroll.bonus")}</h4>
          <p className="muted" style={{ fontSize: 12, margin: "0 0 6px" }}>{t("payroll.bonusHint")}</p>
          <div className="row">
            <Field className="grow" label={t("payroll.bonusMetric")}>
              <select value={form.bonus_metric} onChange={(e) => setForm({ ...form, bonus_metric: e.target.value })}>
                <option value="RUNNING_METERS">{t("payroll.metric_RUNNING_METERS")}</option>
                <option value="WORK_AMOUNT">{t("payroll.metric_WORK_AMOUNT")}</option>
              </select>
            </Field>
            <Field style={{ width: 150 }} label={t("payroll.bonusThreshold")}>
              <input type="number" min="0" step="any" value={form.bonus_threshold} placeholder={t("payroll.noBonus")}
                onChange={(e) => setForm({ ...form, bonus_threshold: e.target.value })} />
            </Field>
            <Field style={{ width: 150 }} label={t("payroll.bonusAmount")}>
              <input type="number" min="0" step="any" value={form.bonus_amount} placeholder="0"
                onChange={(e) => setForm({ ...form, bonus_amount: e.target.value })} />
            </Field>
          </div>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="row" style={{ gap: 8 }}>
            <button className="secondary" onClick={() => setForm(null)}>{t("common.cancel")}</button>
            <button onClick={save}>{t("common.save")}</button>
          </div>
        </div>
      )}

      {schemes === null ? (
        <p className="muted">{t("common.loading")}</p>
      ) : schemes.length === 0 ? (
        <p className="muted">{t("payroll.rulesNone")}</p>
      ) : (
        schemes.map((s) => (
          <div key={s.id} className="crow" style={{ borderBottom: "1px solid var(--hairline)", alignItems: "flex-start" }}>
            <span style={{ minWidth: 0 }}>
              <strong>{t("payroll.rulesSince", { month: monthLabel(s.valid_from) })}</strong>
              <div className="muted" style={{ fontSize: 13 }}>
                {t("payroll.salary")}: {formatMoney(s.salary)}
                {s.rates.length > 0 && " · " + s.rates.map((r) => `${t(`payroll.work_${r.work}`)} ${formatNumber(r.percent, { max: 2 })} %`).join(", ")}
                {s.bonus_threshold != null && Number(s.bonus_amount) > 0 &&
                  " · " + t("payroll.bonusLine", {
                    amount: formatMoney(s.bonus_amount),
                    threshold: formatNumber(s.bonus_threshold, { max: 2 }),
                    metric: t(`payroll.metricShort_${s.bonus_metric}`),
                  })}
              </div>
            </span>
            {!readOnly && (
              <span className="row" style={{ gap: 4, margin: 0 }}>
                <button className="ghost" onClick={() => edit(s)} aria-label={t("common.edit")}><Icon name="pencil" size={16} /></button>
                <button className="ghost" onClick={() => remove(s)} aria-label={t("common.delete")}><Icon name="trash" size={16} /></button>
              </span>
            )}
          </div>
        ))
      )}
    </Modal>
  );
}
