import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const dayOf = (iso) => (iso ? new Date(iso).toLocaleDateString("sv-SE") : "");
const today = () => new Date().toLocaleDateString("sv-SE");

// Правка ошибочно заведённого чека. Меняются только те поля, которые не двигают
// деньги и склад: наименование, клиент, дата заказа. Состав так не правится —
// пересчёт позиций тянет за собой списание материала, себестоимость по партиям
// FIFO и принятые оплаты; ошибочный состав исправляется удалением и повторным
// вводом. Это же и написано в подсказке внизу, чтобы не искать в документации.
const num = (v) => Number(v) || 0;
const trim = (v) => String(+Number(v).toFixed(4));

export default function EditReceiptModal({ receipt, onClose, onSaved }) {
  const { t } = useTranslation();
  const { toast } = useUI();

  const [title, setTitle] = useState(receipt.title || "");
  const [clientId, setClientId] = useState(receipt.client || "");
  const [orderDate, setOrderDate] = useState(dayOf(receipt.created_at));
  const [clients, setClients] = useState([]);
  const [busy, setBusy] = useState(false);

  // Состав чека. Возвращённые строки не показываем: их материал уже вернулся на
  // склад, и правкой количества это не описывается — там был возврат, а не
  // опечатка.
  const [lines, setLines] = useState(() =>
    (receipt.items || [])
      .filter((i) => !i.is_returned)
      .map((i) => ({
        id: i.id,
        name: i.material_name || i.service_name || "—",
        quantity: String(+Number(i.quantity).toFixed(4)),
        price: String(+Number(i.price_per_item).toFixed(2)),
        remove: false,
      })),
  );

  useEffect(() => {
    api.get("/clients/clients/").then((r) => setClients(r.data.results)).catch(() => {});
  }, []);

  const setLine = (id, patch) =>
    setLines((ls) => ls.map((l) => (l.id === id ? { ...l, ...patch } : l)));

  const newTotal = lines.reduce(
    (s, l) => (l.remove ? s : s + Math.ceil(num(l.quantity) * num(l.price))),
    0,
  );
  const oldTotal = Math.round(Number(receipt.total_price) || 0);

  // Что реально изменилось — то и отправляем. Гонять неизменённые строки через
  // склад незачем: каждая правка списывает и возвращает материал заново.
  function changedLines() {
    const orig = new Map(
      (receipt.items || []).map((i) => [
        i.id,
        { q: String(+Number(i.quantity).toFixed(4)), p: String(+Number(i.price_per_item).toFixed(2)) },
      ]),
    );
    return lines
      .filter((l) => {
        const o = orig.get(l.id);
        return l.remove || !o || o.q !== l.quantity || o.p !== l.price;
      })
      .map((l) =>
        l.remove
          ? { id: l.id, remove: true }
          : { id: l.id, quantity: num(l.quantity), price_per_item: num(l.price) },
      );
  }

  async function save() {
    setBusy(true);
    try {
      const items = changedLines();
      if (items.length) {
        await api.post(`/sales/receipts/${receipt.id}/edit-items/`, { items });
      }
      const { data } = await api.patch(`/sales/receipts/${receipt.id}/`, {
        title: title.trim(),
        client: clientId || null,
        order_date: orderDate,
      });
      toast(t("receipts.editSaved"));
      onSaved?.(data);
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("receipts.editTitle", { number: receipt.order_number })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy}>{t("common.save")}</button>
        </>
      }
    >
      <div className="field">
        <label>{t("checkout.orderTitle")}</label>
        <input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
      </div>

      <div className="field">
        <label>{t("checkout.client")}</label>
        <select value={clientId ?? ""} onChange={(e) => setClientId(e.target.value)}>
          <option value="">{t("receipts.noClient")}</option>
          {clients.map((c) => (
            <option key={c.id} value={c.id}>{c.display_name}</option>
          ))}
        </select>
      </div>

      <div className="field">
        <label>{t("checkout.orderDate")}</label>
        <input
          type="date"
          value={orderDate}
          max={today()}
          onChange={(e) => setOrderDate(e.target.value)}
        />
        <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
          {t("receipts.editDateHint")}
        </p>
      </div>

      {/* Состав заказа: количество и цена строки. Ошибаются не только в
          названии — лишний лист, лишний квадратный метр, цена не та. Склад,
          себестоимость и итог пересчитываются на сервере. */}
      {lines.length > 0 && (
        <div className="field">
          <label>{t("receipts.editItems")}</label>
          {lines.map((l) => (
            <div
              key={l.id}
              className="row"
              style={{ margin: "0 0 6px", gap: 6, alignItems: "flex-end", opacity: l.remove ? 0.45 : 1 }}
            >
              <div className="field grow" style={{ margin: 0 }}>
                <label style={{ fontSize: 12 }}>{l.name}</label>
                <input
                  type="number"
                  step="any"
                  min="0"
                  value={l.quantity}
                  disabled={l.remove}
                  onChange={(e) => setLine(l.id, { quantity: e.target.value })}
                />
              </div>
              <div className="field" style={{ margin: 0, width: 110 }}>
                <label style={{ fontSize: 12 }}>{t("receipts.editItemPrice")}</label>
                <input
                  type="number"
                  step="any"
                  min="0"
                  value={l.price}
                  disabled={l.remove}
                  onChange={(e) => setLine(l.id, { price: e.target.value })}
                />
              </div>
              <button
                className={l.remove ? "secondary row-btn" : "ghost row-btn row-danger"}
                style={{ marginBottom: 8 }}
                onClick={() => setLine(l.id, { remove: !l.remove })}
                title={l.remove ? t("common.cancel") : t("common.delete")}
              >
                {l.remove ? t("receipts.editItemRestore") : t("common.delete")}
              </button>
            </div>
          ))}
          <div className="crow">
            <span className="k">{t("common.total")}</span>
            <span>
              {newTotal !== oldTotal && (
                <span className="muted" style={{ textDecoration: "line-through", marginRight: 8 }}>
                  {oldTotal.toLocaleString("ru-RU")}
                </span>
              )}
              <strong>{newTotal.toLocaleString("ru-RU")} сом</strong>
            </span>
          </div>
          {newTotal < Math.round(Number(receipt.amount_paid) || 0) && (
            <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {t("receipts.editItemsChangeHint", {
                amount: (Math.round(Number(receipt.amount_paid) || 0) - newTotal).toLocaleString("ru-RU"),
              })}
            </p>
          )}
        </div>
      )}

      <p className="muted" style={{ fontSize: 12 }}>{t("receipts.editItemsHint")}</p>
    </Modal>
  );
}
