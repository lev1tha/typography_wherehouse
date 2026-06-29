import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Приём оплаты долга по чеку: полная или частичная сумма. По умолчанию — весь
// долг; можно ввести часть, остаток останется долгом. Возвращает обновлённый чек.
export default function PayDebtModal({ receipt, onClose, onPaid }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const debt = Math.round(Number(receipt.debt) || 0);
  const [amount, setAmount] = useState(String(debt));
  const [busy, setBusy] = useState(false);

  const a = Number(amount);
  const valid = a > 0;
  const left = debt - a;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    try {
      const { data } = await api.post(`/sales/receipts/${receipt.id}/pay/`, { amount: a });
      toast(t("receipts.paymentAccepted"));
      onPaid?.(data);
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("receipts.acceptPayment")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("receipts.acceptShort")}</button>
        </>
      }
    >
      <div className="crow">
        <span className="k">{t("receipts.debt")}</span>
        <strong style={{ color: "var(--danger)" }}>{debt.toLocaleString("ru-RU")} сом</strong>
      </div>
      <div className="field" style={{ marginTop: 10 }}>
        <label>{t("receipts.payAmount")}</label>
        <input type="number" min="0" value={amount} onChange={(e) => setAmount(e.target.value)} autoFocus />
        <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("receipts.payHint")}</p>
      </div>
      {valid && left > 0 && (
        <div className="muted" style={{ fontSize: 13 }}>
          {t("receipts.debtAfter")}: <strong style={{ color: "var(--danger)" }}>{left.toLocaleString("ru-RU")} сом</strong>
        </div>
      )}
      {valid && left <= 0 && (
        <div style={{ fontSize: 13, color: "var(--ok, #067647)" }}>{t("receipts.debtClosed")}</div>
      )}
    </Modal>
  );
}
