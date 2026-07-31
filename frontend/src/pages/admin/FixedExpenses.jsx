import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const CATS = ["RENT", "UTILITIES", "INTERNET", "OTHER"];
const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const today = () => new Date().toISOString().slice(0, 10);

function Stat({ label, value }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

// Постоянные расходы — теперь такой же список записей, как «Покупки»:
// аренда, коммуналка, интернет и прочие вносятся отдельными строками с датой,
// поэтому видна история по месяцам, а не одна вечная сумма.
export default function FixedExpenses({ embedded = false }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [period, setPeriod] = useState({ year: new Date().getFullYear(), month: null });
  const [form, setForm] = useState({ category: "RENT", name: "", amount: "", spent_at: today(), note: "" });
  const [editing, setEditing] = useState(null);

  function load() {
    const params = period.month ? { year: period.year, month: period.month } : {};
    api
      .get("/finance/fixed-expenses/", { params })
      .then((r) => setRows(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [period.year, period.month]);

  function add() {
    if (!form.amount) return toast(t("expenses.needAmount"), "error");
    api
      .post("/finance/fixed-expenses/", { ...form, amount: Number(form.amount) })
      .then(() => {
        setForm({ category: form.category, name: "", amount: "", spent_at: form.spent_at, note: "" });
        load();
        toast(t("expenses.added"));
      })
      .catch(() => toast(t("common.error"), "error"));
  }

  function saveEdit() {
    if (!editing.amount) return toast(t("expenses.needAmount"), "error");
    api
      .patch(`/finance/fixed-expenses/${editing.id}/`, {
        category: editing.category,
        name: editing.name,
        amount: Number(editing.amount),
        spent_at: editing.spent_at,
        note: editing.note || "",
      })
      .then(() => {
        setEditing(null);
        load();
        toast(t("common.saved"));
      })
      .catch(() => toast(t("common.error"), "error"));
  }

  async function del(id) {
    if (!(await confirm(t("expenses.confirmDel")))) return;
    api.delete(`/finance/fixed-expenses/${id}/`).then(load);
  }

  const sumBy = (c) => rows.filter((r) => r.category === c).reduce((s, r) => s + Number(r.amount), 0);
  const total = rows.reduce((s, r) => s + Number(r.amount), 0);

  const columns = [
    { key: "spent_at", label: t("expenses.date") },
    { key: "category", label: t("expenses.category"), render: (r) => t(`fixedCat.${r.category}`) },
    { key: "name", label: t("fixed.forWhat"), render: (r) => r.name || "—" },
    { key: "note", label: t("expenses.note"), render: (r) => (r.note ? <span className="muted">{r.note}</span> : "—") },
    { key: "amount", label: t("expenses.amount"), render: (r) => som(r.amount) },
    {
      key: "actions",
      label: "",
      render: (r) => (
        <div className="row" style={{ gap: 4, margin: 0 }}>
          <button className="ghost" onClick={() => setEditing({ ...r })} aria-label={t("common.edit")}>
            <Icon name="pencil" size={16} />
          </button>
          <button className="ghost" onClick={() => del(r.id)} aria-label={t("common.delete")}>
            <Icon name="trash" size={16} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <>
      {!embedded && <h1>{t("fixed.title")}</h1>}
      <p className="muted">{t("fixed.subtitle")}</p>

      <div className="card" style={{ margin: "12px 0 16px" }}>
        <div className="row">
          <div className="field" style={{ minWidth: 190 }}>
            <label>{t("expenses.category")}</label>
            <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>
              {CATS.map((c) => (
                <option key={c} value={c}>{t(`fixedCat.${c}`)}</option>
              ))}
            </select>
          </div>
          <div className="field grow">
            <label>{t("fixed.forWhat")}</label>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={t("fixed.forWhatPh")}
            />
          </div>
          <div className="field" style={{ width: 160 }}>
            <label>{t("expenses.date")}</label>
            <input type="date" value={form.spent_at} onChange={(e) => setForm({ ...form, spent_at: e.target.value })} />
          </div>
          <div className="field" style={{ width: 150 }}>
            <label>{t("expenses.amount")}</label>
            <input type="number" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
          </div>
          <div className="field" style={{ display: "flex", alignItems: "flex-end" }}>
            <button onClick={add}>{t("common.add")}</button>
          </div>
        </div>
        <div className="field" style={{ marginTop: 4 }}>
          <label>{t("expenses.note")}</label>
          <input
            value={form.note}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
            placeholder={t("fixed.notePh")}
          />
        </div>
      </div>

      <div className="toolbar" style={{ alignItems: "center", marginBottom: 12 }}>
        <MonthPicker value={period} onChange={setPeriod} />
      </div>

      <div className="stat-grid" style={{ marginBottom: 16 }}>
        <Stat label={t("fixed.totalForPeriod")} value={som(total)} />
        {CATS.map((c) => (
          <Stat key={c} label={t(`fixedCat.${c}`)} value={som(sumBy(c))} />
        ))}
      </div>

      <DataTable columns={columns} rows={rows} />

      {editing && (
        <Modal
          title={t("fixed.editTitle")}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setEditing(null)}>{t("common.cancel")}</button>
              <button onClick={saveEdit}>{t("common.save")}</button>
            </>
          }
        >
          <div className="field">
            <label>{t("expenses.category")}</label>
            <select value={editing.category} onChange={(e) => setEditing({ ...editing, category: e.target.value })}>
              {CATS.map((c) => (
                <option key={c} value={c}>{t(`fixedCat.${c}`)}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>{t("fixed.forWhat")}</label>
            <input value={editing.name || ""} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
          </div>
          <div className="field">
            <label>{t("expenses.date")}</label>
            <input type="date" value={editing.spent_at} onChange={(e) => setEditing({ ...editing, spent_at: e.target.value })} />
          </div>
          <div className="field">
            <label>{t("expenses.amount")}</label>
            <input type="number" value={editing.amount} onChange={(e) => setEditing({ ...editing, amount: e.target.value })} />
          </div>
          <div className="field">
            <label>{t("expenses.note")}</label>
            <textarea value={editing.note || ""} onChange={(e) => setEditing({ ...editing, note: e.target.value })} rows={2} />
          </div>
        </Modal>
      )}
    </>
  );
}
