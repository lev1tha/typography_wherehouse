import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD, местная дата
const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

// Оплата поставщику: по накладной или по партии, взятой в долг. Зеркало
// «Принять оплату» у клиента — только деньги уходят, а не приходят.
//
// Счёт НЕ подставляем: заказчик платит поставщикам по-разному, и
// подставленное «наличные» врало бы кассе (то же правило, что в приёмке).
export default function PaySupplierModal({ row, onClose, onPaid }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const debt = Number(row.debt) || 0;
  const [amount, setAmount] = useState(String(debt));
  const [paidOn, setPaidOn] = useState(today());
  const [account, setAccount] = useState("");
  const [busy, setBusy] = useState(false);

  const a = Number(amount);
  const valid = a > 0 && a <= debt && !!account;
  const left = debt - a;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    const url =
      row.kind === "SUPPLY"
        ? `/warehouse/supplies/${row.id}/pay/`
        : `/warehouse/rolls/${row.id}/pay-supplier/`;
    try {
      await api.post(url, { amount: a, account, paid_on: paidOn });
      toast(t("suppliersDebt.paid"));
      onPaid?.();
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("suppliersDebt.payTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("suppliersDebt.pay")}</button>
        </>
      }
    >
      <p style={{ marginTop: 0 }}><strong>{row.label}</strong>{row.supplier ? ` · ${row.supplier}` : ""}</p>
      <div className="crow">
        <span className="k">{t("suppliersDebt.owed")}</span>
        <strong style={{ color: "var(--danger)" }}>{som(debt)}</strong>
      </div>
      <div className="field" style={{ marginTop: 10 }}>
        <label>{t("receipts.payAmount")}</label>
        <input type="number" min="0" max={debt} value={amount} onChange={(e) => setAmount(e.target.value)} autoFocus />
        {a > debt && (
          <p style={{ fontSize: 12, marginTop: 4, color: "var(--danger)" }}>{t("suppliersDebt.tooMuch")}</p>
        )}
      </div>
      <div className="row">
        <div className="field grow" style={{ margin: 0 }}>
          <label>{t("clients.payDate")}</label>
          <input type="date" value={paidOn} max={today()} onChange={(e) => setPaidOn(e.target.value)} />
        </div>
        <div className="field grow" style={{ margin: 0 }}>
          <label>{t("suppliersDebt.account")}</label>
          <select value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="" disabled>{t("suppliersDebt.chooseAccount")}</option>
            <option value="CASH">{t("suppliersDebt.cash")}</option>
            <option value="BANK">{t("suppliersDebt.bank")}</option>
          </select>
        </div>
      </div>
      {a > 0 && a <= debt && (
        <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          {left > 0
            ? <>{t("suppliersDebt.left")}: <strong style={{ color: "var(--danger)" }}>{som(left)}</strong></>
            : <span style={{ color: "var(--ok, #067647)" }}>{t("suppliersDebt.closed")}</span>}
        </div>
      )}
    </Modal>
  );
}
