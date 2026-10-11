import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney, formatNumber } from "../utils/format.js";
import Field from "./Field.jsx";

const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD, местная дата
const som = (n) => formatMoney(n);

// Оплата поставщику: по накладной, по партии, взятой в долг, или АВАНСОМ без
// накладной (`row.kind === "ADVANCE"`). Зеркало «Принять оплату» у клиента —
// только деньги уходят, а не приходят.
//
// Накладная в валюте: сумма вводится в валюте накладной, рядом — курс на день
// оплаты; разница с курсом накладной уйдёт расходом «Курсовая разница».
//
// Счёт НЕ подставляем: заказчик платит поставщикам по-разному, и
// подставленное «наличные» врало бы кассе (то же правило, что в приёмке).
// Тот же раздел денег, что на сервере (`supplier_ledger.record_payment`, RU-N17):
// курсовая разница — целыми сомами, «закрыто долга» — остальное; платёж,
// закрывающий долг, закрывает его до тыйына, а из кассы уходит долг + целая
// разница. Тогда строки кассы (до сома) складываются ровно в «С кассы уйдёт».
const cents = (n) => Math.round(n * 100) / 100;
const halfUp = (n) => Math.sign(n) * Math.round(Math.abs(n));

function splitForeignPayment({ amountFc, rate, supplyRate, debtFc, debt }) {
  let cash = cents(amountFc * rate);
  const closing = amountFc >= debtFc - 0.005;
  const exact = Math.min(closing ? debt : cents(amountFc * supplyRate), debt);
  let fx = halfUp(cash - exact);
  let settled;
  if (closing) {
    settled = exact;
    cash = cents(settled + fx);
  } else {
    settled = cents(cash - fx);
    if (settled > debt) {
      settled = debt;
      fx = cents(cash - settled);
    }
  }
  return { cash, settled, fx };
}

// Курс оплаты дальше этой доли от курса накладной — похоже на опечатку (RU-N18).
const RATE_TOLERANCE = 0.1;

export default function PaySupplierModal({ row, onClose, onPaid }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const advance = row.kind === "ADVANCE";
  const foreign = row.kind === "SUPPLY" && row.currency && row.currency !== "KGS";
  const debt = foreign ? Number(row.debt_foreign) || 0 : Number(row.debt) || 0;
  const [amount, setAmount] = useState(advance ? "" : String(debt));
  const [rate, setRate] = useState("");
  const [paidOn, setPaidOn] = useState(today());
  const [account, setAccount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const a = Number(amount);
  const r = Number(rate);
  const valid = a > 0 && (advance || a <= debt) && !!account && (!foreign || r > 0);
  const left = debt - a;
  // Сколько сом уйдёт и сколько долга по курсу накладной это закроет — тем же
  // правилом, что и сервер (иначе окно и касса расходились на сом).
  const supplyRate = Number(row.supply_rate || 0);
  const split = foreign && a > 0 && r > 0
    ? splitForeignPayment({ amountFc: a, rate: r, supplyRate, debtFc: debt, debt: Number(row.debt) || 0 })
    : { cash: a, settled: a, fx: 0 };
  const { cash, settled, fx } = split;
  const unit = foreign ? row.currency : "сом";
  // Опечатка в курсе (RU-N18): «881» вместо «88,1» — видно до кнопки.
  const rateFar = foreign && r > 0 && supplyRate > 0 && Math.abs(r - supplyRate) > supplyRate * RATE_TOLERANCE;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    try {
      if (advance) {
        await api.post("/warehouse/supplier-payments/", {
          supplier: row.supplier_id, amount: a, account, paid_on: paidOn, note,
        });
        toast(t("suppliers.advancePaid"));
      } else if (row.kind === "SUPPLY") {
        const send = (extra = {}) => api.post(`/warehouse/supplies/${row.id}/pay/`, {
          amount: a, account, paid_on: paidOn, note, ...(foreign ? { rate: r } : {}), ...extra,
        });
        try {
          await send();
        } catch (e) {
          // Курс далеко от курса накладной — сервер просит подтверждения (409).
          if (!(e.response?.status === 409 && e.response.data?.needs_confirmation)) throw e;
          if (!(await confirm(e.response.data.detail))) return;
          await send({ confirm_rate: true });
        }
        toast(t("suppliersDebt.paid"));
      } else {
        await api.post(`/warehouse/rolls/${row.id}/pay-supplier/`, { amount: a, account, paid_on: paidOn });
        toast(t("suppliersDebt.paid"));
      }
      onPaid?.();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={advance ? t("suppliers.advanceTitle") : t("suppliersDebt.payTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>
            {advance ? t("suppliers.advancePay") : t("suppliersDebt.pay")}
          </button>
        </>
      }
    >
      <p style={{ marginTop: 0 }}><strong>{row.label}</strong>{row.supplier ? ` · ${row.supplier}` : ""}</p>
      {advance ? (
        <p className="muted" style={{ fontSize: 13 }}>{t("suppliers.advanceHint")}</p>
      ) : (
        <div className="crow">
          <span className="k">{t("suppliersDebt.owed")}</span>
          <strong style={{ color: "var(--danger-ink)" }}>
            {foreign ? `${formatNumber(debt, { max: 2 })} ${row.currency} · ${som(row.debt)}` : som(debt)}
          </strong>
        </div>
      )}
      <div className="field" style={{ marginTop: 10 }}>
        <label>{t("receipts.payAmount")}, {unit}</label>
        <input
          type="number" min="0" step="any" max={advance ? undefined : debt} value={amount}
          onChange={(e) => setAmount(e.target.value)} autoFocus
        />
        {!advance && a > debt && (
          <p style={{ fontSize: 12, marginTop: 4, color: "var(--danger-ink)" }}>{t("suppliersDebt.tooMuch")}</p>
        )}
      </div>
      {foreign && (
        <Field label={t("supplies.rate", { cur: row.currency })} hint={t("suppliers.payRateHint", { rate: formatNumber(row.supply_rate, { max: 4 }) })}>
          <input type="number" step="any" inputMode="decimal" value={rate} onChange={(e) => setRate(e.target.value)} />
        </Field>
      )}
      {rateFar && (
        <p role="alert" style={{ fontSize: 12, margin: "-4px 0 8px", color: "var(--danger-ink)" }}>
          {t("suppliers.rateFar", {
            rate: formatNumber(r, { max: 4 }),
            base: formatNumber(supplyRate, { max: 4 }),
            pct: formatNumber(((r - supplyRate) / supplyRate) * 100, { max: 0 }),
          })}
        </p>
      )}
      <div className="row">
        <Field className="grow" style={{ margin: 0 }} label={t("clients.payDate")}>
          <input type="date" value={paidOn} max={today()} onChange={(e) => setPaidOn(e.target.value)} />
        </Field>
        <Field className="grow" style={{ margin: 0 }} label={t("suppliersDebt.account")}>
          <select value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="" disabled>{t("suppliersDebt.chooseAccount")}</option>
            <option value="CASH">{t("suppliersDebt.cash")}</option>
            <option value="BANK">{t("suppliersDebt.bank")}</option>
          </select>
        </Field>
      </div>
      {row.kind !== "LOT" && (
        <Field style={{ marginTop: 10 }} label={t("supplies.note")}>
          <input value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} />
        </Field>
      )}
      {foreign && a > 0 && r > 0 && (
        <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          {t("suppliers.payPreview", { cash: som(cash), settled: som(settled) })}
          {Math.abs(fx) >= 0.005 && (
            <strong style={{ color: fx > 0 ? "var(--danger-ink)" : "var(--ok-ink)" }}>
              {" "}{fx > 0 ? t("suppliers.fxLoss", { sum: som(fx) }) : t("suppliers.fxGain", { sum: som(-fx) })}
            </strong>
          )}
        </div>
      )}
      {!advance && a > 0 && a <= debt && (
        <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          {left > 0.004
            ? <>{t("suppliersDebt.left")}: <strong style={{ color: "var(--danger-ink)" }}>{foreign ? `${formatNumber(left, { max: 2 })} ${row.currency}` : som(left)}</strong></>
            : <span style={{ color: "var(--ok-ink)" }}>{t("suppliersDebt.closed")}</span>}
        </div>
      )}
    </Modal>
  );
}
