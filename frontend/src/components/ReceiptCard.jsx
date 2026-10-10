import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useAuth } from "../auth/AuthContext.jsx";
import AddToOrderModal from "./AddToOrderModal.jsx";
import CancelPaymentModal from "./CancelPaymentModal.jsx";
import GiveChangeModal from "./GiveChangeModal.jsx";
import IssueItemsModal from "./IssueItemsModal.jsx";
import Modal from "./Modal.jsx";
import PayDebtModal from "./PayDebtModal.jsx";
import PrintDocs from "./PrintDocs.jsx";
import RefundModal from "./RefundModal.jsx";
import { FulfillmentBadge, PaymentBadge, WarrantyBadge } from "./StatusBadge.jsx";
import { useUI } from "./UIProvider.jsx";
import WriteOffModal from "./WriteOffModal.jsx";
import { issuedLabel, itemSpecParts, itemTitle } from "../utils/itemLabel.js";
import { formatDate, formatDateTime, formatMoney } from "../utils/format.js";
import { lineRuled, receiptRuled, rulesLabel } from "../utils/pricingRules.js";

const som = (n) => formatMoney(n);
const trimQty = (n) => String(+Number(n || 0).toFixed(3));

// Карточка заказа — одно окно на «Чеки» админа и «Чеки» складовщика.
//
// Раньше у админа карточки не было вовсе (только строка таблицы), а у складовщика
// она была своя, урезанная. Всё, что относится к ОДНОМУ заказу — состав с размерами
// деталей, выдача по позициям, оплаты и их отмена, списание долга, отмена возврата,
// пересчёт по прайсу, печать и наряд мастеру, — живёт здесь и показывается по роли:
// складовщик принимает долг и выдаёт, админ ещё и отменяет/списывает/пересчитывает,
// бухгалтер только смотрит и печатает.
//
// Окно не держит копии заказа: он приходит пропсом, а любое действие отдаёт свежий
// чек в `onChange` — страница подменяет им открытый заказ и перечитывает список.
export default function ReceiptCard({ receipt, onClose, onChange }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin, isAccountant, seesMoney } = useAuth();
  // Бухгалтер только смотрит: сервер его записи не примет (403).
  const readOnly = isAccountant;
  const [busy, setBusy] = useState(false);
  const [paying, setPaying] = useState(false);
  const [adding, setAdding] = useState(false);
  const [refunding, setRefunding] = useState(false);
  const [issuing, setIssuing] = useState(false);
  const [writingOff, setWritingOff] = useState(false);
  const [givingChange, setGivingChange] = useState(false);
  const [cancelling, setCancelling] = useState(null); // запись оплаты, которую отменяем
  const [printKind, setPrintKind] = useState(null); // "CHECK" | "WORKORDER"
  const [repriced, setRepriced] = useState(null); // ответ reprice: было/стало

  const items = receipt.items || [];
  const live = receipt.status !== "CANCELLED";
  const payments = receipt.payments || [];
  const hasDebt = Number(receipt.debt) > 0;
  const hasLive = items.some((i) => !i.is_returned);
  const noMoreWork = !live || receipt.payment_status === "REFUNDED";

  // Возвращать есть что, пока чек не отменён и не возвращён целиком.
  const canRefund = !readOnly && !["REFUNDED", "CANCELLED"].includes(receipt.payment_status) && live && hasLive;
  // Дозаказ в возвращённый и отменённый нельзя; выданный отклоняет сервер.
  const canAdd = !readOnly && !noMoreWork;
  const canUnpay =
    isAdmin &&
    (receipt.payment_status === "PAID" || Number(receipt.amount_paid) > 0) &&
    !["REFUNDED", "PARTIALLY_REFUNDED"].includes(receipt.payment_status) &&
    live;
  // Пересчёт по текущему прайсу — только пока по заказу ничего не оплачено,
  // не возвращено и не выдано: иначе цена уже стала частью сделки.
  const canReprice =
    isAdmin &&
    live &&
    receipt.payment_status === "PENDING" &&
    !(Number(receipt.amount_paid) > 0) &&
    !(Number(receipt.refunded_amount) > 0) &&
    !(Number(receipt.change_applied) > 0) &&
    !items.some((i) => i.is_returned) &&
    !["ISSUED", "PARTIALLY_ISSUED"].includes(receipt.fulfillment_status);
  const canIssue =
    !readOnly &&
    receipt.has_service &&
    !noMoreWork &&
    items.some((i) => !i.is_returned && Number(i.issued_qty || 0) < Number(i.quantity));

  const methodLabel = (m) => (["CHANGE", "WRITE_OFF"].includes(m) ? t(`receiptsV2.method${m}`) : t(`checkout.${String(m).toLowerCase()}`));

  async function run(fn) {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      toast(apiError(err, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function setFulfillment(status) {
    // Откат из «Выдан частично» снимает отметки «выдано» по позициям.
    if (receipt.fulfillment_status === "PARTIALLY_ISSUED" && status === "PROCESSING") {
      if (!(await confirm(t("issue.rollbackAsk")))) return;
    }
    return run(async () => {
      const { data } = await api.post(`/sales/receipts/${receipt.id}/set-fulfillment/`, { status });
      onChange(data);
      toast(t("receipts.statusUpdated"));
    });
  }

  async function undoPay() {
    if (!(await confirm(t("receipts.confirmUnpay")))) return;
    run(async () => {
      const { data } = await api.post(`/sales/receipts/${receipt.id}/unpay/`, {});
      onChange(data);
      toast(t("receipts.unpayDone"));
    });
  }

  async function undoRefund(item) {
    if (!(await confirm(t("receiptsV2.undoRefundAsk", { name: itemTitle(item) })))) return;
    run(async () => {
      const { data } = await api.post(`/sales/receipts/${receipt.id}/undo-refund/`, { item_ids: [item.id] });
      onChange(data);
      toast(t("receiptsV2.undoRefundDone"));
    });
  }

  async function reprice() {
    if (!(await confirm(t("reprice.ask", { number: receipt.order_number })))) return;
    run(async () => {
      const { data } = await api.post(`/sales/receipts/${receipt.id}/reprice/`, {});
      const { reprice: result, ...fresh } = data;
      onChange(fresh);
      setRepriced(result || { before: receipt.total_price, after: fresh.total_price, changed: [], skipped: [] });
    });
  }

  // Исходный заказ гарантии открывается в этом же окне.
  const openOrigin = () =>
    run(async () => {
      const { data } = await api.get(`/sales/receipts/${receipt.warranty_of}/`);
      onChange(data);
    });

  // Выдача сдачи не возвращает чек — перечитываем его.
  const afterChange = () =>
    run(async () => {
      setGivingChange(false);
      const { data } = await api.get(`/sales/receipts/${receipt.id}/`);
      onChange(data);
    });

  const warrantyN = receipt.warranty_of_number ?? "—";

  return (
    <>
      <Modal
        title={`${t("checkout.receipt")} №${receipt.order_number}`}
        onClose={onClose}
        footer={
          <>
            {hasDebt && !readOnly && live && (
              <button onClick={() => setPaying(true)} disabled={busy}>{t("receipts.acceptPayment")}</button>
            )}
            {canAdd && (
              <button className="secondary" onClick={() => setAdding(true)} disabled={busy}>
                + {t("receipts.addBtn")}
              </button>
            )}
            {!readOnly && receipt.has_service && !noMoreWork && receipt.fulfillment_status === "PROCESSING" && (
              <button className="secondary" onClick={() => setFulfillment("READY")} disabled={busy}>
                {t("receipts.markReady")}
              </button>
            )}
            {canIssue && (
              <button className="secondary" onClick={() => setIssuing(true)} disabled={busy}>
                {t("issue.btn")}
              </button>
            )}
            {!readOnly && receipt.has_service && !noMoreWork &&
              ["READY", "PARTIALLY_ISSUED"].includes(receipt.fulfillment_status) && (
                <button className="secondary" onClick={() => setFulfillment("ISSUED")} disabled={busy}>
                  {t("receipts.markIssued")}
                </button>
              )}
            {/* Откат прямо в окне чека: сюда заходят разбираться с заказом, а
                промах по «Готово» замечают чаще всего именно здесь. */}
            {!readOnly && receipt.has_service && !noMoreWork && receipt.fulfillment_status !== "PROCESSING" && (
              <button
                className="secondary"
                onClick={() => setFulfillment("PROCESSING")}
                disabled={busy}
                title={t("receipts.rollbackTitle")}
              >
                ← {t("receipts.markProcessing")}
              </button>
            )}
            {canRefund && (
              <button className="danger" onClick={() => setRefunding(true)} disabled={busy}>
                {t("receipts.refund")}
              </button>
            )}
            <button className="secondary" onClick={() => setPrintKind("CHECK")}>{t("print.print")}</button>
            <button className="secondary" onClick={() => setPrintKind("WORKORDER")}>{t("workOrder.btn")}</button>
          </>
        }
      >
        <div className="row" style={{ gap: 8, alignItems: "center", margin: "0 0 8px" }}>
          {receipt.title && <strong>{receipt.title}</strong>}
          {receipt.is_warranty && <WarrantyBadge />}
          {receipt.is_urgent && <span className="badge amber">{t("workOrder.urgent")}</span>}
        </div>

        {/* Гарантийная переделка: чья это переделка, из-за чего и кто виноват. */}
        {receipt.is_warranty && (
          <div className="callout" role="note">
            <strong>{t("warranty.redoOf", { n: warrantyN })}</strong>
            {receipt.warranty_reason && <div>{t("warranty.reason")}: {receipt.warranty_reason}</div>}
            {receipt.warranty_culprit && <div>{t("warranty.culprit")}: {receipt.warranty_culprit}</div>}
            {receipt.warranty_of && (
              <button className="ghost row-btn" onClick={openOrigin} disabled={busy} style={{ marginTop: 6 }}>
                {t("warranty.openOrigin", { n: warrantyN })}
              </button>
            )}
          </div>
        )}

        {(receipt.client_name || receipt.buyer_name) && (
          <div className="crow">
            <span className="k">{t("checkout.client")}</span>
            <span>{receipt.client_name || receipt.buyer_name}</span>
          </div>
        )}
        <div className="crow">
          <span className="k">{t("receipts.date")}</span>
          <span>{formatDateTime(receipt.created_at)}</span>
        </div>

        {/* Строки с единицей и ценой, суммы целыми сомами — как в печатной
            форме и в списке чеков. Под названием — размеры деталей, станок,
            материал работы и сколько уже выдано. */}
        <div className="rc-section">
          {items.map((it) => {
            const unit = it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || "";
            const spec = itemSpecParts(it, t);
            if (it.price_is_manual) spec.push(t("receiptsV2.spec.manualPrice"));
            if (it.client_price) spec.push(t("receiptsV2.spec.clientPrice"));
            if (it.executor_name) spec.push(`${t("checkout2.executor")}: ${it.executor_name}`);
            const issued = it.is_returned ? "" : issuedLabel(it, t);
            return (
              <div className="crow" key={it.id} style={{ alignItems: "flex-start" }}>
                <span>
                  {itemTitle(it, t)}
                  <span className="muted">
                    {" "}× {trimQty(it.quantity)} {unit} · {trimQty(it.price_per_item)}{" "}
                    {t("checkout.perPieceShort", { unit })}
                  </span>
                  {it.is_returned && (
                    <span className="badge warn" style={{ marginLeft: 6 }}>{t("receipts.returned")}</span>
                  )}
                  {spec.length > 0 && <span className="rc-spec">{spec.join(" · ")}</span>}
                  {issued && <span className="rc-spec">{issued}</span>}
                  {it.is_returned && isAdmin && (
                    <button
                      className="ghost row-btn"
                      style={{ marginTop: 4 }}
                      onClick={() => undoRefund(it)}
                      disabled={busy}
                    >
                      ↩ {t("receiptsV2.undoRefund")}
                    </button>
                  )}
                </span>
                <span>
                  {lineRuled(it) && <s className="muted">{som(it.catalog_total)}</s>}{" "}
                  {som(it.line_total)}
                </span>
              </div>
            );
          })}
        </div>

        {receiptRuled(receipt) && (
          <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 8 }}>
            <span className="k">
              {t("checkout.catalogTotal")}
              <span className="muted" style={{ display: "block", fontSize: 12 }}>{rulesLabel(receipt, t)}</span>
            </span>
            <s className="muted">{som(receipt.catalog_total)}</s>
          </div>
        )}
        <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 8 }}>
          <strong>{t("common.total")}</strong>
          <strong>{som(receipt.total_price)}</strong>
        </div>
        {Number(receipt.amount_paid) > 0 && (
          <div className="crow">
            <span className="k">{t("receipts.paid")}</span>
            <span>{som(receipt.amount_paid)}</span>
          </div>
        )}
        {/* Часть заказа, закрытая сдачей с прошлых: без этой строки «оплачено
            3 000» по заказу, за который принесли 2 000, выглядит как ошибка кассы. */}
        {Number(receipt.change_applied) - Number(receipt.advance_applied || 0) > 0 && (
          <div className="crow">
            <span className="k">{t("checkout.changeUsed")}</span>
            <span>{som(Number(receipt.change_applied) - Number(receipt.advance_applied || 0))}</span>
          </div>
        )}
        {Number(receipt.advance_applied) > 0 && (
          <div className="crow">
            <span className="k">{t("checkout2.advanceUsed")}</span>
            <span>{som(receipt.advance_applied)}</span>
          </div>
        )}
        {Number(receipt.change_due) > 0 && (
          <div className="crow">
            <span className="k">{t("receipts.change")}</span>
            <span className="row" style={{ gap: 8, alignItems: "center", margin: 0 }}>
              <strong style={{ color: "var(--accent-ink)" }}>{som(receipt.change_due)}</strong>
              {isAdmin && (
                <button className="secondary row-btn" onClick={() => setGivingChange(true)} disabled={busy}>
                  {t("receipts.changeGive")}
                </button>
              )}
            </span>
          </div>
        )}
        {hasDebt && (
          <div className="crow">
            <span className="k">{t("receipts.debt")}</span>
            <span className="row" style={{ gap: 8, alignItems: "center", margin: 0 }}>
              <strong style={{ color: "var(--danger-ink)" }}>{som(receipt.debt)}</strong>
              {isAdmin && live && (
                <button className="ghost row-btn row-danger" onClick={() => setWritingOff(true)} disabled={busy}>
                  {t("writeOff.btn")}
                </button>
              )}
            </span>
          </div>
        )}
        <div className="crow">
          <span className="k">{t("receipts.status")}</span>
          <PaymentBadge status={receipt.payment_status} />
        </div>
        <div className="crow">
          <span className="k">{t("receipts.method")}</span>
          <span>{t(`checkout.${receipt.payment_method.toLowerCase()}`)}</span>
        </div>
        {receipt.cashier_name && (
          <div className="crow">
            <span className="k">{t("receipts.cashier")}</span>
            <span>
              {receipt.cashier_name}
              {receipt.cashier_role && <span className="muted"> · {receipt.cashier_role}</span>}
            </span>
          </div>
        )}
        {receipt.has_service && (
          <div className="crow">
            <span className="k">{t("receipts.fulfillment")}</span>
            <FulfillmentBadge status={receipt.fulfillment_status} />
          </div>
        )}

        {/* Маржа и себестоимость — тем, кто видит закупочные цифры. После
            гарантийных переделок маржа исходного заказа честнее прежней: материал
            на переделку списан, а выручки с неё нет. */}
        {seesMoney && receipt.margin != null && (
          <>
            <div className="crow">
              <span className="k">{t("receipts.margin")}</span>
              <strong style={{ color: Number(receipt.margin) < 0 ? "var(--danger-ink)" : undefined }}>
                {som(receipt.margin)}
              </strong>
            </div>
            {Number(receipt.cost_total) > 0 && (
              <div className="crow">
                <span className="k">{t("receipts.cost")}</span>
                <span>{som(receipt.cost_total)}</span>
              </div>
            )}
            {Number(receipt.warranty_cost) > 0 && (
              <>
                <div className="crow">
                  <span className="k">{t("warranty.cost")}</span>
                  <span style={{ color: "var(--danger-ink)" }}>{som(receipt.warranty_cost)}</span>
                </div>
                {receipt.margin_net != null && (
                  <div className="crow">
                    <span className="k">{t("warranty.marginNet")}</span>
                    <strong style={{ color: Number(receipt.margin_net) < 0 ? "var(--danger-ink)" : undefined }}>
                      {som(receipt.margin_net)}
                    </strong>
                  </div>
                )}
              </>
            )}
          </>
        )}

        {/* Принятые оплаты долга: кто принял, чем, с каким примечанием. Админ
            может отменить одну запись, не откатывая весь заказ (cash-03). */}
        {payments.length > 0 && (
          <div className="rc-section">
            <h3 className="rc-title">{t("receiptsV2.payments")}</h3>
            {payments.map((p) => (
              <div className="rc-pay" key={p.id}>
                <div>
                  <strong>{som(p.amount)}</strong> · {methodLabel(p.method)}
                  <span className="rc-spec">
                    {formatDate(p.paid_on)}
                    {p.created_by_name && ` · ${t("receiptsV2.acceptedBy", { name: p.created_by_name })}`}
                  </span>
                  {p.note && <span className="rc-spec">{p.note}</span>}
                </div>
                {isAdmin && live && (
                  <button className="ghost row-btn row-danger" onClick={() => setCancelling(p)} disabled={busy}>
                    {t("payCancel.btn")}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
        {/* Оплата, принятая при оформлении, записи не имеет — её снимает только
            «Откат оплаты» целиком. */}
        {canUnpay && (
          <div className="rc-section">
            <button className="secondary" onClick={undoPay} disabled={busy}>↩ {t("receipts.unpay")}</button>
            {payments.length > 0 && (
              <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>{t("payCancel.firstPayHint")}</p>
            )}
          </div>
        )}

        {canReprice && (
          <div className="rc-section">
            <button className="secondary" onClick={reprice} disabled={busy}>{t("reprice.btn")}</button>
            <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>{t("reprice.hint")}</p>
          </div>
        )}
      </Modal>

      {adding && (
        <AddToOrderModal
          receiptId={receipt.id}
          receipt={receipt}
          onClose={() => setAdding(false)}
          onAdded={(data) => {
            setAdding(false);
            onChange(data);
          }}
        />
      )}

      {refunding && (
        <RefundModal
          receipt={receipt}
          onClose={() => setRefunding(false)}
          onDone={(data) => {
            setRefunding(false);
            onChange(data);
          }}
        />
      )}

      {paying && (
        <PayDebtModal
          receipt={receipt}
          onClose={() => setPaying(false)}
          onPaid={(data) => {
            setPaying(false);
            onChange(data);
          }}
        />
      )}

      {issuing && (
        <IssueItemsModal
          receipt={receipt}
          onClose={() => setIssuing(false)}
          onDone={(data) => {
            setIssuing(false);
            onChange(data);
          }}
        />
      )}

      {writingOff && (
        <WriteOffModal
          receipt={receipt}
          onClose={() => setWritingOff(false)}
          onDone={(data) => {
            setWritingOff(false);
            onChange(data);
          }}
        />
      )}

      {cancelling && (
        <CancelPaymentModal
          receipt={receipt}
          payment={cancelling}
          onClose={() => setCancelling(null)}
          onDone={(data) => {
            setCancelling(null);
            onChange(data);
          }}
        />
      )}

      {givingChange && (
        <GiveChangeModal receipt={receipt} onClose={() => setGivingChange(false)} onGiven={afterChange} />
      )}

      {printKind && <PrintDocs receipt={receipt} initial={printKind} onClose={() => setPrintKind(null)} />}

      {/* Итог пересчёта: что подорожало или подешевело и что осталось нетронутым. */}
      {repriced && (
        <Modal
          title={t("reprice.resultTitle", { number: receipt.order_number })}
          onClose={() => setRepriced(null)}
          footer={<button onClick={() => setRepriced(null)}>{t("common.close")}</button>}
        >
          <div className="crow">
            <span className="k">{t("reprice.before")}</span>
            <span>{som(repriced.before)}</span>
          </div>
          <div className="crow">
            <span className="k">{t("reprice.after")}</span>
            <strong>{som(repriced.after)}</strong>
          </div>
          {repriced.changed?.length ? (
            <div className="rc-section">
              <h3 className="rc-title">{t("reprice.changed")}</h3>
              {repriced.changed.map((c) => (
                <div className="crow" key={c.item ?? c.name}>
                  <span>{c.name}</span>
                  <span>
                    <s className="muted">{som(c.was_total)}</s> → <strong>{som(c.now_total)}</strong>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="muted">{t("reprice.unchanged")}</p>
          )}
          {repriced.skipped?.length > 0 && (
            <div className="rc-section">
              <h3 className="rc-title">{t("reprice.skipped")}</h3>
              <ul style={{ margin: "0 0 6px", paddingLeft: 18 }}>
                {repriced.skipped.map((s) => (
                  <li key={s.item ?? s.name}>{s.name}</li>
                ))}
              </ul>
              <p className="muted" style={{ fontSize: 12, margin: 0 }}>{t("reprice.skippedHint")}</p>
            </div>
          )}
        </Modal>
      )}
    </>
  );
}
