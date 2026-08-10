import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

// Выдача сдачи — зеркало приёма оплаты: там деньги пришли, тут ушли.
// Частями можно специально: мелочи в кассе может не хватить и во второй раз,
// «отдал тысячу из полутора» — рабочая ситуация цеха, а не ошибка ввода.
export default function GiveChangeModal({ receipt, onClose, onGiven }) {
  const { t } = useTranslation();
  const { toast } = useUI();

  const due = Math.round(Number(receipt.change_due) || 0);
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);

  const give = amount === "" ? due : Math.round(Number(amount) || 0);
  const left = Math.max(0, due - give);
  const valid = give > 0 && give <= due;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    try {
      await api.post(`/sales/receipts/${receipt.id}/give-change/`, {
        ...(amount === "" ? {} : { amount: give }),
      });
      toast(t("receipts.changeDone", { amount: som(give) }));
      onGiven?.();
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("receipts.changeTitle", { number: receipt.order_number })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("receipts.changeGive")}</button>
        </>
      }
    >
      <div className="crow">
        <span className="k">{t("receipts.change")}</span>
        <strong style={{ color: "var(--accent-strong)" }}>{som(due)}</strong>
      </div>

      <div className="field" style={{ marginTop: 10 }}>
        <label>{t("receipts.changeAmount")}</label>
        <div className="row" style={{ margin: 0 }}>
          <input
            className="grow"
            type="number"
            min="0"
            max={due}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder={String(due)}
            autoFocus
          />
          <button className="secondary" onClick={() => setAmount(String(due))}>
            {t("receipts.changeAll")}
          </button>
        </div>
        <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("receipts.changeHint")}</p>
      </div>

      {left > 0 && (
        <div className="crow">
          <span className="k">{t("receipts.changeLeft")}</span>
          <strong style={{ color: "var(--accent-strong)" }}>{som(left)}</strong>
        </div>
      )}
    </Modal>
  );
}
