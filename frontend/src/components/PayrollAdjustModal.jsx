import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field, { focusFirstInvalid } from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Удержание из зарплаты: штраф или брак. Уменьшает «к выдаче» и расход на
// зарплату месяца (ущерб возвращён цеху). За брак можно указать номер записи
// списания в журнале склада — по ней видно, за что удержали.
export default function PayrollAdjustModal({ employee, month, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [form, setForm] = useState({ reason: "DEFECT", amount: "", note: "", inventory_log: "" });
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);

  async function save() {
    if (!(Number(form.amount) > 0)) {
      setErrors({ amount: t("payroll.needAmount") });
      return focusFirstInvalid();
    }
    setErrors({});
    setBusy(true);
    try {
      await api.post("/finance/payroll/adjustments/", {
        employee: employee.id, month, reason: form.reason, amount: form.amount, note: form.note,
        inventory_log: form.inventory_log ? Number(form.inventory_log) : null,
      });
      toast(t("common.saved"));
      onDone();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("payroll.adjustTitle", { name: employee.name })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy}>{t("common.save")}</button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("payroll.adjustHint")}</p>
      <div className="row">
        <Field className="grow" label={t("payroll.reason")}>
          <select value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })}>
            <option value="DEFECT">{t("payroll.reason_DEFECT")}</option>
            <option value="FINE">{t("payroll.reason_FINE")}</option>
            <option value="OTHER">{t("payroll.reason_OTHER")}</option>
          </select>
        </Field>
        <Field className="grow" label={t("payroll.amount")} required error={errors.amount}>
          <input type="number" step="any" inputMode="decimal" autoFocus value={form.amount}
            onChange={(e) => setForm({ ...form, amount: e.target.value })} />
        </Field>
      </div>
      {form.reason === "DEFECT" && (
        <Field label={t("payroll.writeoffRef")} hint={t("payroll.writeoffRefHint")}>
          <input type="number" min="1" value={form.inventory_log}
            onChange={(e) => setForm({ ...form, inventory_log: e.target.value })} />
        </Field>
      )}
      <Field label={t("expenses.note")}>
        <input value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })}
          placeholder={t("payroll.adjustNotePh")} />
      </Field>
    </Modal>
  );
}
