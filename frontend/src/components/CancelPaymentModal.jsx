import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatDate, formatMoney } from "../utils/format.js";

// Отмена ОДНОЙ принятой оплаты долга (cash-03, админ). Раньше ошибочную оплату
// можно было откатить только целиком («Откат оплаты»), а вместе с ней уходила и
// история остальных платежей. Здесь снимается ровно выбранная запись: в кассу
// ложится встречная запись (исходная остаётся в книге), в журнале — кто и почему.
//
// Тем же окном отменяют оплату входящего долга (D-141): `url` — его адрес,
// `title` и `hint` — свои подписи; без них окно про оплату заказа.
export default function CancelPaymentModal({ receipt, payment, onClose, onDone, url, title, hint }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      const body = { payment: payment.id };
      if (reason.trim()) body.reason = reason.trim();
      const { data } = await api.post(url || `/sales/receipts/${receipt.id}/cancel-payment/`, body);
      toast(t("payCancel.done"));
      onDone?.(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={title || t("payCancel.title", { number: receipt.order_number })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          <button className="danger" onClick={submit} disabled={busy}>
            {busy ? t("common.loading") : t("payCancel.submit")}
          </button>
        </>
      }
    >
      <p style={{ marginTop: -6 }}>
        {t("payCancel.ask", { amount: formatMoney(payment.amount), date: formatDate(payment.paid_on) })}
      </p>
      <p className="muted" style={{ fontSize: 13 }}>{hint || t("payCancel.hint")}</p>
      <Field label={t("payCancel.reason")} optional optionalLabel={t("common.optional")}>
        <input
          value={reason}
          maxLength={200}
          onChange={(e) => setReason(e.target.value)}
          placeholder={t("payCancel.reasonPh")}
          autoFocus
        />
      </Field>
    </Modal>
  );
}
