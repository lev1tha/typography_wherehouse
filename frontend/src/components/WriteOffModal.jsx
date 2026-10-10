import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { fieldErrors } from "../utils/fieldErrors.js";
import { formatMoney } from "../utils/format.js";

// Списание безнадёжного долга (cash-09, админ). Клиент не заплатит — а долг
// висит в «Дебиторке» и портит цифры. Списанием деньги в кассу не приходят: долг
// просто закрывается, а в ОПиУ появляется расход «Безнадёжные долги». Поэтому
// причина обязательна — по ней потом вспоминают, почему деньги не пришли.
export default function WriteOffModal({ receipt, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const debt = Math.round(Number(receipt.debt) || 0);
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);

  const sum = amount === "" ? debt : Number(amount);
  const amountBad = amount !== "" && !(sum > 0 && sum <= debt);

  async function submit() {
    if (!note.trim()) return setErrors({ note: t("writeOff.needNote") });
    if (amountBad) return;
    setBusy(true);
    setErrors({});
    try {
      const body = { note: note.trim() };
      if (amount !== "") body.amount = sum;
      const { data } = await api.post(`/sales/receipts/${receipt.id}/write-off/`, body);
      toast(t("writeOff.done", { amount: formatMoney(sum) }));
      onDone?.(data);
    } catch (e) {
      const fe = fieldErrors(e);
      setErrors(fe);
      if (!fe.note && !fe.amount) toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("writeOff.title", { number: receipt.order_number })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          <button className="danger" onClick={submit} disabled={busy || amountBad}>
            {busy ? t("common.loading") : t("writeOff.submit")}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("writeOff.hint")}</p>
      <div className="crow">
        <span className="k">{t("receipts.debt")}</span>
        <strong style={{ color: "var(--danger-ink)" }}>{formatMoney(debt)}</strong>
      </div>
      <Field
        style={{ marginTop: 10 }}
        label={t("writeOff.amount")}
        hint={t("writeOff.amountHint")}
        error={errors.amount || (amountBad ? t("writeOff.amountBad", { max: formatMoney(debt) }) : undefined)}
      >
        <input
          type="number"
          min="0"
          max={debt}
          inputMode="decimal"
          value={amount}
          placeholder={String(debt)}
          onChange={(e) => setAmount(e.target.value)}
          autoFocus
        />
      </Field>
      <Field label={t("writeOff.note")} required error={errors.note}>
        <input
          value={note}
          maxLength={255}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t("writeOff.notePh")}
        />
      </Field>
    </Modal>
  );
}
