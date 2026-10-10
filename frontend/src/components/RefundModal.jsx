import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useAuth } from "../auth/AuthContext.jsx";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { fieldErrors } from "../utils/fieldErrors.js";
import { itemSpecParts, itemTitle } from "../utils/itemLabel.js";
import { formatMoney } from "../utils/format.js";

// Возврат по чеку — целиком или ОТДЕЛЬНЫМИ позициями.
//
// Бэкенд принимал `item_ids` давно, но интерфейс всегда слал пустой список,
// то есть возвращал весь чек, и то только из складского раздела: у админа в
// «Чеках» кнопки не было вовсе. А самый частый случай — клиент вернул один
// лист из трёх. Здесь отмечают, что именно вернули (по умолчанию — всё):
// материал этих строк уходит обратно на склад, деньги — расходом кассы (не
// больше, чем по чеку принимали), остальные строки живут дальше.
const som = (n) => formatMoney(n);

// Часть количества (cash-09, волна 2) — у материала и у работ без размеров.
// Сумма части — по правилу сервера: остающееся вверх до сома, возвращается
// остаток, итог заказа не меняется (`split_line_for_refund`).
const canSplit = (it) => it.type === "MATERIAL" || !(Number(it.width) > 0 || Number(it.length) > 0);
const qtyOf = (it) => Number(it.quantity) || 0;
function partValue(it, back) {
  const q = qtyOf(it);
  if (!(back > 0) || back >= q) return Number(it.line_total || 0);
  const kept = Math.ceil(Math.round((q - back) * Number(it.price_per_item) * 1e6) / 1e6);
  return Math.max(0, Number(it.line_total || 0) - kept);
}

export default function RefundModal({ receipt, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const { isAdmin } = useAuth();
  const items = useMemo(() => (receipt.items || []).filter((i) => !i.is_returned), [receipt.items]);
  const [picked, setPicked] = useState(() => new Set(items.map((i) => i.id)));
  // Сколько вернуть по строке: пусто — всё количество.
  const [backQty, setBackQty] = useState({});
  const partOf = (it) => {
    const v = Number(String(backQty[it.id] ?? "").replace(",", "."));
    return canSplit(it) && v > 0 && v < qtyOf(it) ? v : null;
  };
  const [account, setAccount] = useState(""); // "" — с того счёта, куда пришли деньги
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);

  const allPicked = picked.size === items.length;
  const sum = items
    .filter((i) => picked.has(i.id))
    .reduce((s, i) => s + (partOf(i) ? partValue(i, partOf(i)) : Number(i.line_total || 0)), 0);
  // Деньгами отдают ровно переплату относительно того, что у клиента ОСТАЁТСЯ
  // на руках, — так же считает сервер (`refund_receipt`): неоплаченный заказ
  // денег не возвращает, оплаченный целиком — стоимость возвращённых строк,
  // оплаченный частично — только то, что выходит за стоимость оставшихся.
  const paid = Number(receipt.amount_paid || 0);
  const total = Number(receipt.total_price || 0);
  const refundedBefore = Number(receipt.refunded_amount || 0);
  const excess = (refunded) => Math.max(0, paid - (total - refunded));
  const moneyBack = Math.max(0, excess(refundedBefore + sum) - excess(refundedBefore));
  const reasonRequired = !isAdmin && paid > 0;

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
    if (reasonRequired && !reason.trim()) {
      return setErrors({ reason: t("receiptsV2.refundReasonReq") });
    }
    setBusy(true);
    setErrors({});
    try {
      // Все строки — как раньше: «вернуть чек целиком» без списка. Счёт и
      // причину шлём только когда их выбрали — пустые сервер всё равно примет,
      // но запись в журнале тогда была бы без причины.
      const partial = items.filter((i) => picked.has(i.id) && partOf(i));
      const body =
        allPicked && !partial.length
          ? {}
          : {
              item_ids: [...picked].filter((id) => !partial.some((p) => p.id === id)),
              ...(partial.length ? { quantities: partial.map((p) => ({ id: p.id, quantity: String(partOf(p)) })) } : {}),
            };
      if (account) body.method = account;
      if (reason.trim()) body.reason = reason.trim();
      const { data } = await api.post(`/sales/receipts/${receipt.id}/refund/`, body);
      toast(t("receipts.refundDone"));
      onDone?.(data);
    } catch (e) {
      // 400 {reason: […]} — подсвечиваем поле «Причина», остальное — тостом.
      const fe = fieldErrors(e);
      setErrors(fe);
      if (!fe.reason) toast(apiError(e, t("common.error")), "error");
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
            style={{ padding: 0, height: "auto", color: "var(--accent-ink)" }}
            onClick={() => setPicked(allPicked ? new Set() : new Set(items.map((i) => i.id)))}
          >
            {allPicked ? t("receipts.refundNone") : t("receipts.refundAll")}
          </button>
        </div>
        {items.map((it) => {
          const name = itemTitle(it, t);
          const spec = itemSpecParts(it, t);
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
                  {spec.length > 0 && <span className="rc-spec">{spec.join(" · ")}</span>}
                </span>
              </span>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                {picked.has(it.id) && canSplit(it) && qtyOf(it) > 1 && (
                  <input
                    type="number"
                    inputMode="decimal"
                    min="0"
                    step="any"
                    value={backQty[it.id] ?? ""}
                    placeholder={String(+qtyOf(it).toFixed(3))}
                    onChange={(e) => setBackQty((m) => ({ ...m, [it.id]: e.target.value }))}
                    aria-label={`${t("receiptsV2.refundQty")}: ${name}`}
                    title={t("receiptsV2.refundQty")}
                    style={{ width: 72, minHeight: 0, height: 28, padding: "2px 6px" }}
                  />
                )}
                <strong>{som(partOf(it) ? partValue(it, partOf(it)) : it.line_total)}</strong>
              </span>
            </label>
          );
        })}
        {!items.length && <p className="muted">{t("receipts.refundEmpty")}</p>}
      </div>

      {/* Счёт нужен, только когда часть возврата уходит деньгами. */}
      {moneyBack > 0 && (
        <Field label={t("receiptsV2.refundAccount")} hint={t("receiptsV2.refundAccountHint")}>
          <select value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="">{t("receiptsV2.accountDefault")}</option>
            <option value="CASH">{t("checkout.cash")}</option>
            <option value="MBANK">{t("checkout.mbank")}</option>
            <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
          </select>
        </Field>
      )}

      <Field
        label={t("receiptsV2.refundReason")}
        required={reasonRequired}
        optional={!reasonRequired}
        optionalLabel={t("common.optional")}
        hint={reasonRequired ? t("receiptsV2.refundReasonHint") : undefined}
        error={errors.reason}
      >
        <input
          value={reason}
          maxLength={255}
          onChange={(e) => setReason(e.target.value)}
          placeholder={t("receiptsV2.refundReasonPh")}
        />
      </Field>

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
