import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney, formatMoneyExact } from "../utils/format.js";
import { useIdempotency } from "../utils/idempotency.js";
import { parseNumber } from "../utils/pasteTable.js";

const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD, местная дата

// Окна денежных действий над клиентом (CLI-05, CLI-06, CLI-07, CLI-03).
// Каждое — маленькая форма: сервер всё проверяет сам (права, закрытый период,
// суммы), а окно показывает его ответ рядом с полем или тостом — «кнопка не
// нажимается» из-за молчаливого 400 здесь невозможна.
//
// Сумма — текстовое поле (RU-N14): «5 000» и «5 000,50» так, как их пишут и
// копируют из Excel; number-поле браузера пробел и запятую не принимает.
// Разбор — тот же `parseNumber`, что во «Входящих остатках»; тыйыны и потолок
// суммы проверяет сервер.

/** Принять аванс без заказа: приход в кассу + сальдо в пользу клиента.
 *  У клиента есть долг — по умолчанию деньги сначала гасят его (D-165), авансом
 *  остаётся только остаток сверх долга. Повтор после обрыва уходит с тем же
 *  ключом и второй раз не проводится (CLI-14). */
export function AdvanceModal({ client, isAdmin, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const debt = Number(client.debt) || 0;
  const [amount, setAmount] = useState("");
  const [method, setMethod] = useState("CASH");
  const [paidOn, setPaidOn] = useState(today());
  const [note, setNote] = useState("");
  const [offset, setOffset] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const idem = useIdempotency();

  async function submit() {
    const value = parseNumber(amount);
    if (!(value > 0)) return setErr(t("clients.advanceBad"));
    setErr("");
    setBusy(true);
    try {
      const body = {
        amount: String(value), method, note, offset_debt: debt > 0 && offset,
        ...(isAdmin ? { paid_on: paidOn } : {}),
      };
      const { data } = await api.post(`/clients/clients/${client.id}/advances/`, body, {
        headers: { "Idempotency-Key": idem.keyFor(JSON.stringify([client.id, body])) },
      });
      idem.done();
      if (data.idempotent_replay) toast(t("clients.repeatIgnored"));
      else if (Number(data.to_debt) > 0) {
        toast(t("clients.advanceOffsetDone", {
          sum: formatMoney(data.accepted),
          debt: formatMoney(data.to_debt),
          advance: formatMoney(data.advance?.amount || 0),
        }));
      } else toast(t("clients.advanceDone", { sum: formatMoney(data.amount) }));
      onDone?.(data);
    } catch (e) {
      idem.failed(e);
      setErr(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`${t("clients.advanceBtn")} — ${client.display_name}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy}>{t("receipts.acceptShort")}</button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("clients.advanceHint")}</p>
      <Field label={t("clients.advanceAmount")} error={err}>
        <input
          type="text" inputMode="decimal" value={amount} autoFocus placeholder="5 000"
          onChange={(e) => setAmount(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
      </Field>
      <div className="row">
        {isAdmin && (
          <Field className="grow" style={{ margin: 0 }} label={t("clients.payDate")}>
            <input type="date" value={paidOn} max={today()} onChange={(e) => setPaidOn(e.target.value)} />
          </Field>
        )}
        <Field className="grow" style={{ margin: 0 }} label={t("checkout.paymentMethod")}>
          <select value={method} onChange={(e) => setMethod(e.target.value)}>
            <option value="CASH">{t("checkout.cash")}</option>
            <option value="MBANK">{t("checkout.mbank")}</option>
            <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
          </select>
        </Field>
      </div>
      <Field style={{ marginTop: 10 }} label={t("clients.advanceNote")} optional optionalLabel={t("common.optional")}>
        <input value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} />
      </Field>
      {debt > 0 && (
        <label className="crow" style={{ cursor: "pointer", alignItems: "flex-start", gap: 10, marginTop: 12 }}>
          <input
            type="checkbox" style={{ width: 18, height: 18, minHeight: 0, marginTop: 2 }}
            checked={offset} onChange={(e) => setOffset(e.target.checked)}
          />
          <span>
            <strong>{t("clients.advanceOffset", { debt: formatMoneyExact(debt) })}</strong>
            <span className="muted" style={{ display: "block", fontSize: 12 }}>
              {offset ? t("clients.advanceOffsetHint") : t("clients.advanceOffsetOff")}
            </span>
          </span>
        </label>
      )}
    </Modal>
  );
}

/** Списать безнадёжный долг клиента (только админ): без денег, расход в ОПиУ. */
export function WriteOffModal({ client, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const debt = Number(client.debt) || 0;
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState({});

  async function submit() {
    const errs = {};
    if (!note.trim()) errs.note = t("clients.writeOffReasonNeeded");
    const value = amount.trim() === "" ? null : parseNumber(amount);
    if (amount.trim() !== "" && !(value > 0)) errs.amount = t("clients.advanceBad");
    setErr(errs);
    if (Object.keys(errs).length) return;
    const sum = value == null ? debt : value;
    if (!(await confirm(t("clients.writeOffConfirm", { sum: formatMoneyExact(sum), name: client.display_name })))) return;
    setBusy(true);
    try {
      const { data } = await api.post(`/clients/clients/${client.id}/pay-debt/`, {
        method: "WRITE_OFF", note: note.trim(), ...(value == null ? {} : { amount: String(value) }),
      });
      toast(t("clients.writeOffDone", { sum: formatMoney(data.paid) }));
      onDone?.(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("clients.writeOffTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button className="danger" onClick={submit} disabled={busy}>{t("clients.writeOffBtn")}</button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("clients.writeOffHint")}</p>
      <Field label={t("clients.writeOffAmount", { sum: formatMoneyExact(debt) })} error={err.amount}>
        <input type="text" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
      </Field>
      <Field label={t("clients.writeOffReason")} required error={err.note}>
        <input
          value={note} maxLength={255} autoFocus placeholder={t("clients.writeOffReasonPh")}
          onChange={(e) => setNote(e.target.value)}
        />
      </Field>
    </Modal>
  );
}

/** Отметка о выплате реферального бонуса: сумма и дата (только запись, касса не двигается). */
export function BonusPayModal({ referrer, item, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const due = Number(item.bonus.due) || 0;
  const [amount, setAmount] = useState(String(due));
  const [paidOn, setPaidOn] = useState(today());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    if (!(Number(amount) > 0)) return setErr(t("clients.advanceBad"));
    setErr("");
    setBusy(true);
    try {
      await api.post(`/clients/clients/${referrer.id}/referral-bonus/pay/`, {
        referred: item.id, amount, paid_on: paidOn,
      });
      toast(t("clients.bonusPayDone"));
      onDone?.();
    } catch (e) {
      setErr(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("clients.bonusPayTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy}>{t("clients.bonusPay")}</button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>
        {t("clients.bonusPayFor", { name: item.display_name, sum: formatMoney(due) })}
      </p>
      <Field label={t("clients.bonusPayAmount")} error={err}>
        <input
          type="number" min="0" inputMode="decimal" value={amount} autoFocus
          onChange={(e) => setAmount(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
      </Field>
      <Field label={t("clients.bonusPayDate")}>
        <input type="date" value={paidOn} max={today()} onChange={(e) => setPaidOn(e.target.value)} />
      </Field>
      <p className="muted" style={{ fontSize: 12 }}>{t("clients.bonusPayNote")}</p>
    </Modal>
  );
}

/** Общие правила клиентов (только админ): общий лимит долга и приём денег складовщиком. */
export function ClientSettingsModal({ settings, onClose, onSaved }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [limit, setLimit] = useState(settings.default_credit_limit == null ? "" : String(+Number(settings.default_credit_limit)));
  const [takes, setTakes] = useState(!!settings.storekeeper_takes_debt);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit() {
    if (limit !== "" && !(Number(limit) >= 0)) return setErr(t("clients.creditLimitBad"));
    setErr("");
    setBusy(true);
    try {
      const { data } = await api.patch("/clients/settings/", {
        default_credit_limit: limit === "" ? null : limit,
        storekeeper_takes_debt: takes,
      });
      toast(t("clients.settingsSaved"));
      onSaved?.(data);
    } catch (e) {
      setErr(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("clients.settingsTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy}>{t("common.save")}</button>
        </>
      }
    >
      <Field label={t("clients.settingsDefaultLimit")} hint={t("clients.settingsDefaultLimitHint")} error={err}>
        <input
          type="number" min="0" inputMode="decimal" value={limit} placeholder="—"
          onChange={(e) => setLimit(e.target.value)}
        />
      </Field>
      <label className="crow" style={{ cursor: "pointer", alignItems: "flex-start", gap: 10, marginTop: 14 }}>
        <input
          type="checkbox" style={{ width: 18, height: 18, minHeight: 0, marginTop: 2 }}
          checked={takes} onChange={(e) => setTakes(e.target.checked)}
        />
        <span>
          <strong>{t("clients.settingsStorekeeperDebt")}</strong>
          <span className="muted" style={{ display: "block", fontSize: 12 }}>{t("clients.settingsStorekeeperDebtHint")}</span>
        </span>
      </label>
      <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>{t("clients.debtWarnDaysNote")}</p>
    </Modal>
  );
}
