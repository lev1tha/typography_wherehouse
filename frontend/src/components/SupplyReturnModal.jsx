import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney, formatNumber } from "../utils/format.js";

const today = () => new Date().toLocaleDateString("sv-SE");
const som = (n) => formatMoney(n);

// «Вернуть поставщику» (G1-N2): сколько по каждой строке уезжает назад. Склад
// уменьшается по цене партии (потерь в прибыли нет). Накладная остаётся как в
// бумаге, возврат — строкой с минусом датой возврата (D-171, с RU-N23 — и в
// открытом месяце): закуп этого дня и долг по накладной меньше на стоимость
// возвращённого. Деньги — либо вернулись на счёт, либо остались кредитом у
// поставщика (виден в сальдо).
export default function SupplyReturnModal({ supply, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [qty, setQty] = useState({});
  const [returnedOn, setReturnedOn] = useState(today());
  const [mode, setMode] = useState("CREDIT");
  const [account, setAccount] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const rows = supply.lines.map((l) => {
    const q = Number(qty[l.id]) || 0;
    const total = Number(l.return_total) || 0;
    const value = total > 0 ? Math.round(((Number(l.cost) * q) / total) * 100) / 100 : 0;
    const max = Number(l.returnable) || 0;
    return { l, q, value, max, bad: q < 0 || q > max + 1e-9 };
  });
  const picked = rows.filter((r) => r.q > 0);
  const value = picked.reduce((s, r) => s + r.value, 0);
  const anyBad = rows.some((r) => r.bad);
  const paid = Number(supply.paid_total) || 0;
  // Накладная закрытого месяца (D-171): её сумма не меняется, возврат идёт
  // датой возврата — долг считается от суммы за вычетом таких возвратов.
  const closed = !!supply.period_closed;
  const newTotal = Number(supply.total_cost) - (Number(supply.returned_after) || 0) - value;
  // Столько заплачено сверх новой суммы накладной — его и можно получить назад.
  const refundable = Math.max(0, Math.min(value, paid - newTotal));
  const valid = picked.length > 0 && !anyBad && (mode === "CREDIT" || (refundable > 0 && !!account));

  async function submit() {
    if (!valid) return;
    setBusy(true);
    try {
      const { data } = await api.post(`/warehouse/supplies/${supply.id}/return/`, {
        lines: picked.map((r) => ({ line: r.l.id, quantity: r.q })),
        returned_on: returnedOn,
        mode,
        ...(mode === "REFUND" ? { account } : {}),
        note,
      });
      toast(t("supplyReturn.done"));
      onDone?.(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      wide
      title={`${t("supplyReturn.title")} · ${supply.number || `#${supply.id}`}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("supplyReturn.confirm")}</button>
        </>
      }
    >
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>{t("supplyReturn.hint")}</p>
      {closed && (
        <p role="note" style={{ fontSize: 13, marginTop: 0 }}>
          <strong>{t("supplyReturn.closedHint")}</strong>
        </p>
      )}
      <div className="table-scroll">
        <table className="table plain-table">
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("supplyReturn.onShelf")}</th>
              <th style={{ width: 150 }}>{t("supplyReturn.qty")}</th>
              <th>{t("supplyReturn.value")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ l, q, value: v, max, bad }) => (
              <tr key={l.id}>
                <td><strong>{l.material_name}</strong></td>
                <td>{formatNumber(max, { max: 2 })} {l.return_unit}</td>
                <td>
                  <input
                    type="number" min="0" step="any" inputMode="decimal"
                    aria-label={`${t("supplyReturn.qty")} · ${l.material_name}`}
                    aria-invalid={bad ? true : undefined}
                    value={qty[l.id] ?? ""}
                    onChange={(e) => setQty((s) => ({ ...s, [l.id]: e.target.value }))}
                    style={bad ? { borderColor: "var(--danger-ink)" } : undefined}
                  />
                  {bad && <div style={{ color: "var(--danger-ink)", fontSize: 12 }}>{t("supplyReturn.tooMuch")}</div>}
                </td>
                <td>{q > 0 ? som(v) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="row" style={{ marginTop: 12, gap: 12, flexWrap: "wrap" }}>
        <Field style={{ margin: 0, width: 170 }} label={t("supplyReturn.date")}>
          <input type="date" value={returnedOn} max={today()} onChange={(e) => setReturnedOn(e.target.value)} />
        </Field>
        <Field className="grow" style={{ margin: 0 }} label={t("supplies.note")}>
          <input value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </div>

      <fieldset style={{ border: "1px solid var(--hairline)", borderRadius: 8, margin: "14px 0 0", padding: "8px 12px" }}>
        <legend style={{ fontSize: 13, padding: "0 6px" }}>{t("supplyReturn.money")}</legend>
        <label className="row" style={{ margin: "4px 0", gap: 8, alignItems: "center", cursor: "pointer" }}>
          <input type="radio" name="ret-mode" checked={mode === "CREDIT"} onChange={() => setMode("CREDIT")} />
          <span>{t("supplyReturn.credit")}</span>
        </label>
        <label className="row" style={{ margin: "4px 0", gap: 8, alignItems: "center", cursor: refundable > 0 ? "pointer" : "not-allowed" }}>
          <input
            type="radio" name="ret-mode" disabled={refundable <= 0}
            checked={mode === "REFUND"} onChange={() => setMode("REFUND")}
          />
          <span>
            {t("supplyReturn.refund")}
            {refundable > 0 ? ` · ${som(refundable)}` : ` · ${t("supplyReturn.refundNone")}`}
          </span>
        </label>
        {mode === "REFUND" && (
          <Field style={{ marginTop: 8, width: 220 }} label={t("supplyReturn.account")}>
            <select value={account} onChange={(e) => setAccount(e.target.value)}>
              <option value="" disabled>{t("suppliersDebt.chooseAccount")}</option>
              <option value="CASH">{t("suppliersDebt.cash")}</option>
              <option value="BANK">{t("suppliersDebt.bank")}</option>
            </select>
          </Field>
        )}
      </fieldset>

      {value > 0 && (
        <div className="muted" style={{ fontSize: 13, marginTop: 10 }}>
          {t("supplyReturn.previewClosed", { value: som(value), debt: som(Math.max(0, newTotal - paid)) })}
          {mode === "CREDIT" && paid - newTotal > 0 && (
            <strong style={{ color: "var(--ok-ink)" }}> {t("supplyReturn.creditLeft", { sum: som(paid - newTotal) })}</strong>
          )}
        </div>
      )}
    </Modal>
  );
}
