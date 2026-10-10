import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import ClientPicker from "./ClientPicker.jsx";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { itemTitle } from "../utils/itemLabel.js";
import { formatMoney, formatNumber } from "../utils/format.js";
import { applyRules, itemRules } from "../utils/pricingRules.js";

const dayOf = (iso) => (iso ? new Date(iso).toLocaleDateString("sv-SE") : "");
const today = () => new Date().toLocaleDateString("sv-SE");

// Правка ошибочно заведённого чека. Меняются только те поля, которые не двигают
// деньги и склад: наименование, клиент, дата заказа. Состав так не правится —
// пересчёт позиций тянет за собой списание материала, себестоимость по партиям
// FIFO и принятые оплаты; ошибочный состав исправляется удалением и повторным
// вводом. Это же и написано в подсказке внизу, чтобы не искать в документации.
const num = (v) => Number(v) || 0;
const trim = (v) => String(+Number(v).toFixed(4));
// Цена в окне — цена ДО правил прайса (`catalog_price`): минимум, срочность
// и скидка строки пересчитываются от неё сервером по правилам заказа. У строк,
// проданных до правил, `catalog_price` пуст — их цена и есть цена.
const basePrice = (i) => i.catalog_price ?? i.price_per_item;

export default function EditReceiptModal({ receipt, onClose, onSaved }) {
  const { t } = useTranslation();
  const { toast } = useUI();

  const [title, setTitle] = useState(receipt.title || "");
  const [clientId, setClientId] = useState(receipt.client || "");
  const [orderDate, setOrderDate] = useState(dayOf(receipt.created_at));
  const [busy, setBusy] = useState(false);

  // Состав чека. Возвращённые строки не показываем: их материал уже вернулся на
  // склад, и правкой количества это не описывается — там был возврат, а не
  // опечатка.
  const [lines, setLines] = useState(() =>
    (receipt.items || [])
      .filter((i) => !i.is_returned)
      .map((i) => ({
        id: i.id,
        name: itemTitle(i),
        quantity: String(+Number(i.quantity).toFixed(4)),
        price: String(+Number(basePrice(i)).toFixed(2)),
        rules: i.catalog_price != null ? itemRules(i) : null,
        remove: false,
      })),
  );

  const setLine = (id, patch) =>
    setLines((ls) => ls.map((l) => (l.id === id ? { ...l, ...patch } : l)));

  const newTotal = lines.reduce(
    // Строка — вверх до сома, как на сервере (line_total), по своим правилам.
    (s, l) => (l.remove ? s : s + applyRules(num(l.quantity) * num(l.price), l.rules || {})),
    0,
  );
  // Сравниваем с тем, что клиенту осталось платить: итог чека держит и
  // возвращённые строки (возврат уменьшает `refunded_amount`, а не итог), а в
  // окне их нет — иначе у чека с частичным возвратом старая сумма всегда
  // стояла бы зачёркнутой.
  const oldTotal = Math.round((Number(receipt.total_price) || 0) - (Number(receipt.refunded_amount) || 0));

  // Что реально изменилось — то и отправляем. Гонять неизменённые строки через
  // склад незачем: каждая правка списывает и возвращает материал заново.
  function changedLines() {
    const orig = new Map(
      (receipt.items || []).map((i) => [
        i.id,
        { q: String(+Number(i.quantity).toFixed(4)), p: String(+Number(basePrice(i)).toFixed(2)) },
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
      let data = receipt;
      if (items.length) {
        ({ data } = await api.post(`/sales/receipts/${receipt.id}/edit-items/`, { items }));
      }
      // Шлём только то, что поменяли. Раньше уходили все три поля разом, и
      // правка одного количества переставляла время заказа на полдень (дата
      // «изменилась» на ту же самую) — чек уезжал в хронологии, а журнал
      // действий писал «client, order_date, title» на пустом месте.
      const meta = {};
      if (title.trim() !== (receipt.title || "")) meta.title = title.trim();
      if (String(clientId || "") !== String(receipt.client || "")) meta.client = clientId || null;
      if (orderDate && orderDate !== dayOf(receipt.created_at)) meta.order_date = orderDate;
      if (Object.keys(meta).length) {
        ({ data } = await api.patch(`/sales/receipts/${receipt.id}/`, meta));
      }
      toast(t("receipts.editSaved"));
      onSaved?.(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
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
      <Field label={t("checkout.orderTitle")}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
      </Field>

      {/* Клиент — поиском по серверу: прежний <select> знал только первых 25
          клиентов, и чек нельзя было переписать на остальных. Показываем
          текущего клиента заказа, даже если его нет в выдаче поиска. */}
      <Field label={t("checkout.client")}>
        <ClientPicker
          value={clientId ?? ""}
          valueLabel={receipt.client && String(clientId) === String(receipt.client) ? receipt.client_name : undefined}
          noneLabel={t("receipts.noClient")}
          onChange={(id) => setClientId(id)}
        />
      </Field>

      <Field label={t("checkout.orderDate")} hint={t("receipts.editDateHint")}>
        <input
          type="date"
          value={orderDate}
          max={today()}
          onChange={(e) => setOrderDate(e.target.value)}
        />
      </Field>

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
              <Field className="grow" style={{ margin: 0 }} label={l.name}>
                <input
                  type="number"
                  step="any"
                  min="0"
                  inputMode="decimal"
                  value={l.quantity}
                  disabled={l.remove}
                  onChange={(e) => setLine(l.id, { quantity: e.target.value })}
                />
              </Field>
              <Field style={{ margin: 0, width: 110 }} label={t("receipts.editItemPrice")}>
                <input
                  type="number"
                  step="any"
                  min="0"
                  inputMode="decimal"
                  value={l.price}
                  disabled={l.remove}
                  onChange={(e) => setLine(l.id, { price: e.target.value })}
                />
              </Field>
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
                  {formatNumber(oldTotal)}
                </span>
              )}
              <strong>{formatMoney(newTotal)}</strong>
            </span>
          </div>
          {newTotal < Math.round(Number(receipt.amount_paid) || 0) && (
            <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {t("receipts.editItemsChangeHint", {
                amount: formatNumber(Math.round(Number(receipt.amount_paid) || 0) - newTotal),
              })}
            </p>
          )}
        </div>
      )}

      <p className="muted" style={{ fontSize: 12 }}>{t("receipts.editItemsHint")}</p>
    </Modal>
  );
}
