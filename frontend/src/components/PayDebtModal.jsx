import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD, местная дата

// Приём оплаты долга по чеку: полная или частичная сумма. По умолчанию — весь
// долг; можно ввести часть, остаток останется долгом. Возвращает обновлённый чек.
//
// Дата и способ — те же, что в общей выплате по клиенту: деньги берут в цехе, а
// проводят позже, поэтому оплату можно записать задним числом.
export default function PayDebtModal({ receipt, onClose, onPaid }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const debt = Math.round(Number(receipt.debt) || 0);
  const [amount, setAmount] = useState(String(debt));
  const [paidOn, setPaidOn] = useState(today());
  const [method, setMethod] = useState(receipt.payment_method || "CASH");
  const [busy, setBusy] = useState(false);

  const a = Number(amount);
  const valid = a > 0;
  const left = debt - a;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    try {
      const { data } = await api.post(`/sales/receipts/${receipt.id}/pay/`, {
        amount: a,
        paid_on: paidOn,
        method,
      });
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
      <div className="row">
        <div className="field grow" style={{ margin: 0 }}>
          <label>{t("clients.payDate")}</label>
          <input
            type="date"
            value={paidOn}
            max={today()}
            onChange={(e) => setPaidOn(e.target.value)}
          />
        </div>
        <div className="field grow" style={{ margin: 0 }}>
          <label>{t("checkout.paymentMethod")}</label>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="CASH">{t("checkout.cash")}</option>
            <option value="MBANK">{t("checkout.mbank")}</option>
            <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
          </select>
        </div>
      </div>
      {paidOn !== today() && (
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 8px" }}>{t("clients.backdatedHint")}</p>
      )}
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
