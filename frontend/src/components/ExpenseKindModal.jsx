import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { useAuth } from "../auth/AuthContext.jsx";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const today = () => new Date().toISOString().slice(0, 10);

// Дата новой траты по умолчанию: сегодня, если сегодня внутри выбранного
// периода, иначе первый день периода. Иначе, открыв июль в августе и внеся
// аренду, пользователь не увидел бы её в том же отчёте — она молча упала бы
// в август.
function defaultDate(period) {
  const now = today();
  if (!period?.date_from) return now;
  if (period.date_to && now > period.date_to) return period.date_from;
  if (now < period.date_from) return period.date_from;
  return now;
}

// Диалог одного вида расхода: все траты по нему за выбранный период, добавление
// прямо здесь и правка строки на месте. Раньше для этого нужно было листать
// страницу до отдельного раздела и искать нужную категорию в выпадающем списке.
export default function ExpenseKindModal({ kind, period, onClose, onChanged, onEditKind }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  // Бухгалтер сюда заходит смотреть: запись в финансах сервер ему запрещает.
  const { isAccountant: readOnly } = useAuth();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({ name: "", amount: "", spent_at: defaultDate(period), note: "" });
  const [editing, setEditing] = useState(null);

  // У зарплат в это поле пишется имя сотрудника — мастера и резчики не заводятся
  // как пользователи системы.
  const isSalary = kind.code === "SALARY";
  const nameLabel = isSalary ? t("salary.employee") : t("fixed.forWhat");
  const namePlaceholder = isSalary ? t("salary.employeePh") : t("fixed.forWhatPh");

  function load() {
    setLoading(true);
    api
      .get("/finance/expense-entries/", { params: { kind: kind.id, ...(period || {}) } })
      .then((r) => setRows(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"))
      .finally(() => setLoading(false));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [kind.id, period?.date_from, period?.date_to]);

  function add() {
    if (!form.amount) return toast(t("expenses.needAmount"), "error");
    api
      .post("/finance/expense-entries/", {
        kind: kind.id,
        name: form.name,
        amount: Number(form.amount),
        spent_at: form.spent_at,
        note: form.note,
      })
      .then(() => {
        setForm({ name: "", amount: "", spent_at: form.spent_at, note: "" });
        load();
        onChanged?.();
        toast(t("expenses.added"));
      })
      .catch(() => toast(t("common.error"), "error"));
  }

  function saveEdit() {
    if (!editing.amount) return toast(t("expenses.needAmount"), "error");
    api
      .patch(`/finance/expense-entries/${editing.id}/`, {
        name: editing.name,
        amount: Number(editing.amount),
        spent_at: editing.spent_at,
        note: editing.note || "",
      })
      .then(() => {
        setEditing(null);
        load();
        onChanged?.();
        toast(t("common.saved"));
      })
      .catch(() => toast(t("common.error"), "error"));
  }

  async function del(row) {
    if (!(await confirm(t("expenses.confirmDel")))) return;
    api
      .delete(`/finance/expense-entries/${row.id}/`)
      .then(() => {
        load();
        onChanged?.();
      })
      .catch(() => toast(t("common.error"), "error"));
  }

  const total = rows.reduce((s, r) => s + Number(r.amount || 0), 0);

  return (
    <Modal
      title={kind.name}
      onClose={onClose}
      footer={
        <>
          {/* Настройка вида и ввод трат — админские: бухгалтеру сервер их не
              даст, и кнопка, которая гарантированно ответит 403, только
              путает. Он остаётся с тем, зачем и приходит, — со списком. */}
          {!readOnly && (
            <button className="secondary" onClick={() => onEditKind?.(kind)}>
              {t("kinds.settings")}
            </button>
          )}
          <button onClick={onClose}>{t("common.close")}</button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -4 }}>
        {period?.date_from
          ? t("kinds.periodHint", { from: period.date_from, to: period.date_to })
          : t("kinds.allTimeHint")}
        {!kind.in_profit && ` · ${t("kinds.notInProfitHint")}`}
      </p>

      {!readOnly && (
      <div className="card" style={{ margin: "10px 0 14px", background: "var(--primary-soft)" }}>
        <div className="row">
          <div className="field grow">
            <label>{nameLabel}</label>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={namePlaceholder}
            />
          </div>
          <div className="field" style={{ width: 150 }}>
            <label>{t("expenses.date")}</label>
            <input
              type="date"
              value={form.spent_at}
              onChange={(e) => setForm({ ...form, spent_at: e.target.value })}
            />
          </div>
          <div className="field" style={{ width: 130 }}>
            <label>{t("expenses.amount")}</label>
            <input
              type="number"
              value={form.amount}
              onChange={(e) => setForm({ ...form, amount: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && add()}
            />
          </div>
          <div className="field" style={{ display: "flex", alignItems: "flex-end" }}>
            <button onClick={add}>{t("common.add")}</button>
          </div>
        </div>
        <div className="field" style={{ marginTop: 2, marginBottom: 0 }}>
          <label>{t("expenses.note")}</label>
          <input
            value={form.note}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
            placeholder={t("expenses.notePh")}
          />
        </div>
      </div>
      )}

      {loading ? (
        <p className="muted">{t("common.loading")}</p>
      ) : rows.length === 0 ? (
        <p className="muted">{t("kinds.empty")}</p>
      ) : (
        <div style={{ maxHeight: 320, overflowY: "auto" }}>
          {rows.map((r) =>
            editing?.id === r.id ? (
              <div key={r.id} className="card" style={{ margin: "6px 0", padding: 12 }}>
                <div className="row">
                  <div className="field grow">
                    <label>{nameLabel}</label>
                    <input
                      value={editing.name || ""}
                      onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                    />
                  </div>
                  <div className="field" style={{ width: 150 }}>
                    <label>{t("expenses.date")}</label>
                    <input
                      type="date"
                      value={editing.spent_at}
                      onChange={(e) => setEditing({ ...editing, spent_at: e.target.value })}
                    />
                  </div>
                  <div className="field" style={{ width: 130 }}>
                    <label>{t("expenses.amount")}</label>
                    <input
                      type="number"
                      value={editing.amount}
                      onChange={(e) => setEditing({ ...editing, amount: e.target.value })}
                    />
                  </div>
                </div>
                <div className="field">
                  <label>{t("expenses.note")}</label>
                  <input
                    value={editing.note || ""}
                    onChange={(e) => setEditing({ ...editing, note: e.target.value })}
                  />
                </div>
                <div className="row" style={{ gap: 8 }}>
                  <button className="secondary" onClick={() => setEditing(null)}>
                    {t("common.cancel")}
                  </button>
                  <button onClick={saveEdit}>{t("common.save")}</button>
                </div>
              </div>
            ) : (
              <div key={r.id} className="crow" style={{ borderBottom: "1px solid var(--hairline)" }}>
                <span style={{ minWidth: 0 }}>
                  <span className="muted" style={{ fontSize: 12 }}>{r.spent_at}</span>
                  {r.name && <> · <strong>{r.name}</strong></>}
                  {r.note && (
                    <div className="muted" style={{ fontSize: 12 }}>{r.note}</div>
                  )}
                </span>
                <span className="row" style={{ gap: 4, margin: 0, alignItems: "center" }}>
                  <strong>{som(r.amount)}</strong>
                  {!readOnly && (
                    <>
                      <button className="ghost" onClick={() => setEditing({ ...r })} aria-label={t("common.edit")}>
                        <Icon name="pencil" size={16} />
                      </button>
                      <button className="ghost" onClick={() => del(r)} aria-label={t("common.delete")}>
                        <Icon name="trash" size={16} />
                      </button>
                    </>
                  )}
                </span>
              </div>
            )
          )}
        </div>
      )}

      <div
        className="crow"
        style={{
          background: "var(--primary-soft)",
          borderRadius: "var(--r-md)",
          padding: "10px 14px",
          marginTop: 10,
        }}
      >
        <strong style={{ color: "var(--accent-strong)" }}>{t("fixed.totalForPeriod")}</strong>
        <strong style={{ color: "var(--accent-strong)" }}>{som(total)}</strong>
      </div>
    </Modal>
  );
}
