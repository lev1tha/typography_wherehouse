import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { itemTitle } from "../utils/itemLabel.js";

// Возврат по чеку — целиком или ОТДЕЛЬНЫМИ позициями.
//
// Бэкенд принимал `item_ids` давно, но интерфейс всегда слал пустой список,
// то есть возвращал весь чек, и то только из складского раздела: у админа в
// «Чеках» кнопки не было вовсе. А самый частый случай — клиент вернул один
// лист из трёх. Здесь отмечают, что именно вернули (по умолчанию — всё):
// материал этих строк уходит обратно на склад, деньги — расходом кассы (не
// больше, чем по чеку принимали), остальные строки живут дальше.
const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

export default function RefundModal({ receipt, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const items = useMemo(() => (receipt.items || []).filter((i) => !i.is_returned), [receipt.items]);
  const [picked, setPicked] = useState(() => new Set(items.map((i) => i.id)));
  const [busy, setBusy] = useState(false);

  const allPicked = picked.size === items.length;
  const sum = items.filter((i) => picked.has(i.id)).reduce((s, i) => s + Number(i.line_total || 0), 0);
  // Деньгами отдают ровно переплату относительно того, что у клиента ОСТАЁТСЯ
  // на руках, — так же считает сервер (`refund_receipt`): неоплаченный заказ
  // денег не возвращает, оплаченный целиком — стоимость возвращённых строк,
  // оплаченный частично — только то, что выходит за стоимость оставшихся.
  const paid = Number(receipt.amount_paid || 0);
  const total = Number(receipt.total_price || 0);
  const refundedBefore = Number(receipt.refunded_amount || 0);
  const excess = (refunded) => Math.max(0, paid - (total - refunded));
  const moneyBack = Math.max(0, excess(refundedBefore + sum) - excess(refundedBefore));

  function toggle(id) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function submit() {
    if (!picked.size) return toast(t("receipts.refundNothing"), "error");
    setBusy(true);
    try {
      // Все строки — как раньше, пустое тело: «вернуть чек целиком».
      const body = allPicked ? {} : { item_ids: [...picked] };
      const { data } = await api.post(`/sales/receipts/${receipt.id}/refund/`, body);
      toast(t("receipts.refundDone"));
      onDone?.(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("receipts.refundTitle", { number: receipt.order_number })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          <button className="danger" onClick={submit} disabled={busy || !picked.size}>
            {busy ? t("common.loading") : t("receipts.refund")}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("receipts.refundHint")}</p>

      <div className="field">
        <div className="row" style={{ justifyContent: "space-between", margin: "0 0 6px" }}>
          <label style={{ margin: 0 }}>{t("receipts.refundPick")}</label>
          <button
            type="button"
            className="ghost"
            style={{ padding: 0, height: "auto", color: "var(--accent-strong)" }}
            onClick={() => setPicked(allPicked ? new Set() : new Set(items.map((i) => i.id)))}
          >
            {allPicked ? t("receipts.refundNone") : t("receipts.refundAll")}
          </button>
        </div>
        {items.map((it) => {
          const name = itemTitle(it, t);
          // Единица — кодом с сервера, подпись из словаря (та же, что в накладной).
          const unit = it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || "";
          return (
            <label
              key={it.id}
              className="crow"
              style={{ cursor: "pointer", borderBottom: "1px solid var(--hairline)" }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <input
                  type="checkbox"
                  style={{ width: 18, height: 18, minHeight: 0 }}
                  checked={picked.has(it.id)}
                  onChange={() => toggle(it.id)}
                />
                <span>
                  {name}{" "}
                  <span className="muted">
                    × {String(+Number(it.quantity).toFixed(3))} {unit}
                  </span>
                </span>
              </span>
              <strong>{som(it.line_total)}</strong>
            </label>
          );
        })}
        {!items.length && <p className="muted">{t("receipts.refundEmpty")}</p>}
      </div>

      <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
        <div className="crow">
          <span className="k">{t("receipts.refundSum")}</span>
          <strong style={{ fontSize: 18 }}>{som(sum)}</strong>
        </div>
        <div className="crow" style={{ paddingTop: 0 }}>
          <span className="k">{t("receipts.refundMoney")}</span>
          <span>{som(moneyBack)}</span>
        </div>
        {sum > moneyBack && (
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>{t("receipts.refundDebtHint")}</p>
        )}
      </div>
    </Modal>
  );
}
