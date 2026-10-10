import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field, { focusFirstInvalid } from "./Field.jsx";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney } from "../utils/format.js";

const som = (n) => formatMoney(n);
const monthLabel = (ym) => (ym ? `${ym.slice(5, 7)}.${ym.slice(0, 4)}` : "");
const thisMonth = () => new Date().toLocaleDateString("sv-SE").slice(0, 7);

// Повторяющиеся траты (PNL-09, G2-N4): «аренда 25 000 каждого 10-го до декабря».
// Система сама заводит обычную трату на каждый месяц, когда день наступил;
// при каждом открытии «Финансов» недостающие месяцы дописываются. Закрытый
// период пропускается; внесённое можно править и удалять как обычную трату.
export default function RecurringExpenses({ kinds, readOnly, onChanged }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState(null);
  const [form, setForm] = useState(null);
  const [errors, setErrors] = useState({});

  function load() {
    api.get("/finance/recurring/")
      .then((r) => setRows(r.data.results || r.data))
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, []);

  // Расписанием ведутся только обычные расходы, проценты и налог.
  const allowed = kinds.filter((k) => ["OPEX", "INTEREST", "TAX"].includes(k.role) && !k.is_archived);

  function start() {
    setErrors({});
    setForm({
      kind: allowed.find((k) => k.code === "RENT")?.id || allowed[0]?.id || "",
      name: "", amount: "", account: "CASH", day: "10", start_month: thisMonth(), until_month: "", is_active: true,
    });
  }

  async function save() {
    const next = {};
    if (!form.kind) next.kind = t("recurring.needKind");
    if (!(Number(form.amount) > 0)) next.amount = t("expenses.needAmount");
    if (!(Number(form.day) >= 1 && Number(form.day) <= 31)) next.day = t("recurring.badDay");
    setErrors(next);
    if (Object.keys(next).length) return focusFirstInvalid();
    const body = {
      kind: Number(form.kind), name: form.name, amount: form.amount, account: form.account,
      day: Number(form.day), start_month: form.start_month, until_month: form.until_month || null,
      is_active: form.is_active,
    };
    try {
      if (form.id) await api.patch(`/finance/recurring/${form.id}/`, body);
      else {
        await api.post("/finance/recurring/", body);
        // Новое правило сразу вносит наступившие месяцы.
        await api.post("/finance/recurring/run/", {});
      }
      setForm(null);
      load();
      onChanged?.();
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function toggle(rule) {
    try {
      await api.patch(`/finance/recurring/${rule.id}/`, { is_active: !rule.is_active });
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function remove(rule) {
    if (!(await confirm(t("recurring.confirmDelete")))) return;
    try {
      await api.delete(`/finance/recurring/${rule.id}/`);
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>{t("recurring.title")}</h3>
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("recurring.hint")}</p>
      {rows === null ? (
        <p className="muted">{t("common.loading")}</p>
      ) : rows.length === 0 ? (
        <p className="muted">{t("recurring.empty")}</p>
      ) : (
        rows.map((r) => (
          <div key={r.id} className="crow" style={{ borderBottom: "1px solid var(--hairline)", alignItems: "flex-start", opacity: r.is_active ? 1 : 0.55 }}>
            <span style={{ minWidth: 0 }}>
              <strong>{r.kind_name}</strong>{r.name ? ` · ${r.name}` : ""}
              <div className="muted" style={{ fontSize: 12 }}>
                {t("recurring.line", {
                  day: r.day, from: monthLabel(r.start_month),
                  until: r.until_month ? monthLabel(r.until_month) : t("recurring.noEnd"),
                  n: r.entries_count,
                })}
                {!r.is_active && ` · ${t("recurring.off")}`}
              </div>
            </span>
            <span className="row" style={{ gap: 4, margin: 0, alignItems: "center" }}>
              <strong>{som(r.amount)}</strong>
              {!readOnly && (
                <>
                  <button className="ghost" onClick={() => { setErrors({}); setForm({ ...r, until_month: r.until_month || "" }); }} aria-label={t("common.edit")}>
                    <Icon name="pencil" size={16} />
                  </button>
                  <button className="ghost row-btn" onClick={() => toggle(r)}>{r.is_active ? t("recurring.disable") : t("recurring.enable")}</button>
                  <button className="ghost" onClick={() => remove(r)} aria-label={t("common.delete")}>
                    <Icon name="trash" size={16} />
                  </button>
                </>
              )}
            </span>
          </div>
        ))
      )}
      {!readOnly && (
        <div className="row" style={{ marginTop: 10 }}>
          <button className="secondary" onClick={start}>+ {t("recurring.add")}</button>
        </div>
      )}

      {form && (
        <Modal
          title={form.id ? t("recurring.edit") : t("recurring.add")}
          onClose={() => setForm(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setForm(null)}>{t("common.cancel")}</button>
              <button onClick={save}>{t("common.save")}</button>
            </>
          }
        >
          <div className="row">
            <Field className="grow" label={t("recurring.kind")} required error={errors.kind}>
              <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
                {allowed.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
              </select>
            </Field>
            <Field className="grow" label={t("fixed.forWhat")}>
              <input value={form.name} placeholder={t("recurring.namePh")} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </Field>
          </div>
          <div className="row">
            <Field className="grow" label={t("expenses.amount")} required error={errors.amount}>
              <input type="number" step="any" inputMode="decimal" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
            </Field>
            <Field style={{ width: 130 }} label={t("recurring.day")} required error={errors.day}>
              <input type="number" min="1" max="31" value={form.day} onChange={(e) => setForm({ ...form, day: e.target.value })} />
            </Field>
            <Field style={{ width: 150 }} label={t("expenses.paidFrom")}>
              <select value={form.account} onChange={(e) => setForm({ ...form, account: e.target.value })}>
                <option value="CASH">{t("expenses.paidCash")}</option>
                <option value="BANK">{t("expenses.paidBank")}</option>
              </select>
            </Field>
          </div>
          <div className="row">
            <Field className="grow" label={t("recurring.from")}>
              <input type="month" value={form.start_month} onChange={(e) => setForm({ ...form, start_month: e.target.value })} />
            </Field>
            <Field className="grow" label={t("recurring.until")} hint={t("recurring.untilHint")}>
              <input type="month" value={form.until_month} onChange={(e) => setForm({ ...form, until_month: e.target.value })} />
            </Field>
          </div>
          <p className="muted" style={{ fontSize: 12 }}>{t("recurring.formHint")}</p>
        </Modal>
      )}
    </div>
  );
}
