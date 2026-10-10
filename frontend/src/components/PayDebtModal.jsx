import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useAuth } from "../auth/AuthContext.jsx";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { fieldErrors } from "../utils/fieldErrors.js";
import { formatMoney } from "../utils/format.js";
import { useIdempotency } from "../utils/idempotency.js";

const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD, местная дата
const METHODS = ["CASH", "MBANK", "DEMIRBANK"];
// Ответ с ошибкой по этим полям показываем у самого поля, а не только тостом.
const FIELDS = ["amount", "paid_on", "note", "method"];

// Приём оплаты долга по чеку: полная или частичная сумма. По умолчанию — весь
// долг; можно ввести часть, остаток останется долгом. Возвращает обновлённый чек.
//
// Дата и способ — те же, что в общей выплате по клиенту: деньги берут в цехе, а
// проводят позже, поэтому оплату можно записать задним числом. Задним числом —
// только админ: складовщик тоже принимает долг (CLI-08), но его оплата всегда
// сегодняшняя, и поля даты у него нет.
//
// «Зачесть сдачу клиента» (cash-08): у клиента бывает сдача по другим заказам —
// его же деньги, которые цех ещё не отдал. Вместо того чтобы отдавать её руками и
// тут же брать обратно, долг закрывается ею, а принесённые наличные идут сверху.
export default function PayDebtModal({ receipt, onClose, onPaid }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin } = useAuth();
  const debt = Math.round(Number(receipt.debt) || 0);
  const [amount, setAmount] = useState(String(debt));
  const [useChange, setUseChange] = useState(false);
  const [paidOn, setPaidOn] = useState(today());
  const [method, setMethod] = useState(
    METHODS.includes(receipt.payment_method) ? receipt.payment_method : "CASH",
  );
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  // Сколько у клиента сдачи по другим заказам: undefined — ещё грузится,
  // null — не узнали (карточка клиента недоступна), число — сумма.
  const [avail, setAvail] = useState(receipt.client ? undefined : 0);
  // Потерянный ответ + повторное нажатие не должны принять оплату дважды.
  const idem = useIdempotency();

  useEffect(() => {
    if (!receipt.client) return undefined;
    let alive = true;
    api
      .get(`/clients/clients/${receipt.client}/`)
      .then((r) => alive && setAvail(Math.max(0, Math.round(Number(r.data.change_due) || 0))))
      .catch(() => alive && setAvail(null));
    return () => {
      alive = false;
    };
  }, [receipt.client]);

  // Чекбокс нужен, когда есть что зачитывать. Не знаем сколько (null) — всё равно
  // показываем: сервер сам возьмёт сколько есть, а закрывать глаза на возможность
  // хуже, чем показать лишний чекбокс.
  const canUseChange = avail === null || avail > 0;

  function toggleChange(on) {
    setUseChange(on);
    // С зачётом «принесли наличными» по умолчанию пусто — весь остаток после
    // сдачи; без него по умолчанию весь долг, как было.
    setAmount(on ? "" : String(debt));
  }

  const a = Number(amount);
  const cashEmpty = useChange && amount === "";
  const valid = cashEmpty || (amount !== "" && Number.isFinite(a) && (useChange ? a >= 0 : a > 0));
  // Как разложит сервер: с зачётом сдача берёт то, что наличные не покрыли
  // (но не больше, чем есть у клиента); без «наличных» — сдача идёт первой, а
  // наличные закрывают остаток. Лишнее сверх долга уходит клиенту сдачей.
  const needFromChange = cashEmpty ? debt : Math.max(debt - a, 0);
  const changePart = useChange ? Math.min(avail || 0, needFromChange) : 0;
  const cashPart = cashEmpty ? debt - changePart : Math.min(a, debt - changePart);
  const left = debt - changePart - cashPart;
  const over = cashEmpty ? 0 : Math.max(0, a - cashPart);
  // Сколько сдачи у клиента, не знаем (карточка недоступна), — остаток не считаем.
  const knowsLeft = !useChange || avail !== null;

  async function submit() {
    if (!valid) return;
    setBusy(true);
    setErrors({});
    const base = { method };
    if (!cashEmpty) base.amount = a;
    if (useChange) base.use_change = true;
    if (isAdmin) base.paid_on = paidOn;
    if (note.trim()) base.note = note.trim();
    const send = async (extra = {}) => {
      const body = { ...base, ...extra };
      const { data } = await api.post(`/sales/receipts/${receipt.id}/pay/`, body, {
        headers: { "Idempotency-Key": idem.keyFor(JSON.stringify([receipt.id, body])) },
      });
      return data;
    };
    try {
      let data;
      try {
        data = await send();
      } catch (e) {
        // Сумма в разы больше долга — лишний ноль в «5000» вместо «500» не должен
        // тихо уйти в сдачу: просим подтвердить и повторяем с флагом.
        const warning = e.response?.status === 409 && e.response.data?.needs_confirmation
          ? (e.response.data.warnings || []).find((w) => w.code === "overpay")
          : null;
        if (!warning) throw e;
        idem.failed(e);
        const wAmount = Number(warning.amount);
        const wDebt = Number(warning.debt);
        const text = wAmount > 0 && wDebt > 0
          ? t("receiptsV2.overpayAsk", {
              amount: formatMoney(wAmount),
              debt: formatMoney(wDebt),
              times: (wAmount / wDebt).toFixed(1),
            })
          : warning.message;
        if (!(await confirm(text))) return;
        data = await send({ confirm_overpay: true });
      }
      idem.done();
      toast(t("receipts.paymentAccepted"));
      onPaid?.(data);
    } catch (e) {
      idem.failed(e);
      const fe = fieldErrors(e);
      setErrors(fe);
      if (!Object.keys(fe).some((k) => FIELDS.includes(k))) toast(apiError(e, t("common.error")), "error");
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
        <strong style={{ color: "var(--danger-ink)" }}>{formatMoney(debt)}</strong>
      </div>

      {receipt.client && canUseChange && (
        <div className="field" style={{ marginTop: 10 }}>
          <label style={{ display: "flex", alignItems: "center", gap: 8, margin: 0 }}>
            <input
              type="checkbox"
              style={{ width: 20, height: 20, minHeight: 0 }}
              checked={useChange}
              onChange={(e) => toggleChange(e.target.checked)}
            />
            {t("receiptsV2.useChange")}
            {avail > 0 && <strong style={{ color: "var(--accent-ink)" }}>{formatMoney(avail)}</strong>}
          </label>
          <p className="field-hint">
            {avail === null ? t("receiptsV2.useChangeUnknown") : t("receiptsV2.useChangeHint")}
          </p>
        </div>
      )}

      <Field
        style={{ marginTop: 10 }}
        label={useChange ? t("receiptsV2.cashBrought") : t("receipts.payAmount")}
        required={!useChange}
        hint={useChange ? t("receiptsV2.cashBroughtHint") : t("receipts.payHint")}
        error={errors.amount || (amount !== "" && !valid ? t("cash.needAmount") : undefined)}
      >
        <input
          type="number"
          min="0"
          inputMode="decimal"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          placeholder={useChange ? String(Math.max(0, debt - Math.min(avail || 0, debt))) : undefined}
          autoFocus
        />
      </Field>
      <div className="row">
        {/* Задним числом принимает только админ: у складовщика сервер такую дату
            отклонил бы (403), поэтому и поля у него нет. */}
        {isAdmin && (
          <Field className="grow" style={{ margin: 0 }} label={t("clients.payDate")} error={errors.paid_on}>
            <input
              type="date"
              value={paidOn}
              max={today()}
              onChange={(e) => setPaidOn(e.target.value)}
            />
          </Field>
        )}
        <Field className="grow" style={{ margin: 0 }} label={t("checkout.paymentMethod")} error={errors.method}>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="CASH">{t("checkout.cash")}</option>
            <option value="MBANK">{t("checkout.mbank")}</option>
            <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
          </select>
        </Field>
      </div>
      {isAdmin && paidOn !== today() && (
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 8px" }}>{t("clients.backdatedHint")}</p>
      )}
      <Field
        style={{ marginTop: 10 }}
        label={t("receiptsV2.payNote")}
        optional
        optionalLabel={t("common.optional")}
        error={errors.note}
      >
        <input
          value={note}
          maxLength={255}
          onChange={(e) => setNote(e.target.value)}
          placeholder={t("receiptsV2.payNotePh")}
        />
      </Field>
      {valid && useChange && changePart > 0 && (
        <div className="muted" style={{ fontSize: 13 }}>
          {t("receiptsV2.changeCovers")}: <strong>{formatMoney(changePart)}</strong>
        </div>
      )}
      {valid && knowsLeft && left > 0 && (
        <div className="muted" style={{ fontSize: 13 }}>
          {t("receipts.debtAfter")}: <strong style={{ color: "var(--danger-ink)" }}>{formatMoney(left)}</strong>
        </div>
      )}
      {valid && knowsLeft && left <= 0 && (
        <div style={{ fontSize: 13, color: "var(--ok-ink)" }}>{t("receipts.debtClosed")}</div>
      )}
      {valid && over > 0 && (
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
          {t("receiptsV2.payOverHint", { sum: formatMoney(over) })}
        </p>
      )}
    </Modal>
  );
}
