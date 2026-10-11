import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field, { focusFirstInvalid } from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney } from "../utils/format.js";

const today = () => new Date().toLocaleDateString("sv-SE");

// Аванс или расчёт сотруднику. Деньги уходят из кассы (или со счёта банка) и
// гасят начисление за выбранный месяц; в ОПиУ выплата не идёт — зарплата там
// уже начислена за месяц. Месяц по умолчанию — тот, что открыт в ведомости.
export default function PayrollPayModal({ employee, employees, month, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [form, setForm] = useState({
    employee: employee?.id ?? "",
    kind: "PAYOUT",
    amount: employee && Number(employee.to_pay) > 0 ? String(Number(employee.to_pay)) : "",
    paid_on: today(),
    period: month,
    account: "CASH",
    note: "",
  });
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);

  async function save() {
    const next = {};
    if (!form.employee) next.employee = t("payroll.needEmployee");
    if (!(Number(form.amount) > 0)) next.amount = t("payroll.needAmount");
    setErrors(next);
    if (Object.keys(next).length) return focusFirstInvalid();
    setBusy(true);
    const body = {
      employee: Number(form.employee), kind: form.kind, amount: form.amount,
      paid_on: form.paid_on, period: form.period || undefined, account: form.account, note: form.note,
    };
    try {
      try {
        await api.post("/finance/payroll/payments/", body);
      } catch (e) {
        // Больше «к выдаче», за будущий месяц или отключённому (RF-N3, D-185):
        // сервер спрашивает — показываем его вопрос, «да» уходит повтором.
        const ask = e.response?.status === 409 && e.response.data?.needs_confirmation;
        if (!ask) throw e;
        const text = (e.response.data.warnings || []).map((w) => t(`payroll.warn_${w.code}`, {
          defaultValue: w.message,
          period: w.period ? `${w.period.slice(5, 7)}.${w.period.slice(0, 4)}` : "",
          amount: formatMoney(w.amount), toPay: formatMoney(w.to_pay),
        }));
        if (!(await confirm([...text, t("payroll.warnAsk")].join(" ")))) return;
        await api.post("/finance/payroll/payments/", { ...body, confirm_warnings: true });
      }
      toast(t("payroll.paid"));
      onDone();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("payroll.payTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy}>{t("payroll.payConfirm")}</button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("payroll.payHint")}</p>
      {!employee && (
        <Field label={t("salary.employee")} required error={errors.employee}>
          <select value={form.employee} onChange={(e) => setForm({ ...form, employee: e.target.value })}>
            <option value="">—</option>
            {employees.filter((e) => e.is_active).map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
          </select>
        </Field>
      )}
      {employee && <p style={{ margin: "0 0 8px" }}><strong>{employee.name}</strong></p>}
      <div className="row">
        <Field className="grow" label={t("payroll.kind")}>
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
            <option value="ADVANCE">{t("payroll.advance")}</option>
            <option value="PAYOUT">{t("payroll.payout")}</option>
          </select>
        </Field>
        <Field className="grow" label={t("payroll.amount")} required error={errors.amount}>
          <input type="number" step="any" inputMode="decimal" autoFocus value={form.amount}
            onChange={(e) => setForm({ ...form, amount: e.target.value })} />
        </Field>
      </div>
      <div className="row">
        <Field className="grow" label={t("payroll.paidOn")}>
          <input type="date" value={form.paid_on} max={today()}
            onChange={(e) => setForm({ ...form, paid_on: e.target.value })} />
        </Field>
        <Field className="grow" label={t("payroll.forMonth")} hint={t("payroll.forMonthHint")}>
          <input type="month" value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value })} />
        </Field>
        <Field className="grow" label={t("expenses.paidFrom")}>
          <select value={form.account} onChange={(e) => setForm({ ...form, account: e.target.value })}>
            <option value="CASH">{t("expenses.paidCash")}</option>
            <option value="BANK">{t("expenses.paidBank")}</option>
          </select>
        </Field>
      </div>
      <Field label={t("expenses.note")}>
        <input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
      </Field>
    </Modal>
  );
}
