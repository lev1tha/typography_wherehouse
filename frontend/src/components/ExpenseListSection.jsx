import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import DataTable from "./DataTable.jsx";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

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

// Список трат по группе видов расхода — то, что раньше было отдельными
// страницами «Постоянные расходы», «Зарплаты» и «Покупки». Диалог по клику на
// строку отчёта удобен для одного вида, но всю историю за месяц одним списком
// он не показывает, поэтому списки остались.
export default function ExpenseListSection({ title, subtitle, kinds, period, onChanged }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({ kind: "", name: "", amount: "", spent_at: today(), note: "" });

  const kindIds = kinds.map((k) => k.id);
  const kindById = Object.fromEntries(kinds.map((k) => [k.id, k]));
  const isSalary = (kindId) => kindById[kindId]?.code === "SALARY";

  function load() {
    if (!kindIds.length) return setRows([]);
    api
      .get("/finance/expense-entries/", { params: period || {} })
      .then((r) => {
        const all = r.data.results || r.data;
        setRows(all.filter((x) => kindIds.includes(x.kind)));
      })
      .catch(() => toast(t("common.error"), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [period?.date_from, period?.date_to, kindIds.join(",")]);

  // Вид по умолчанию — первый в группе, чтобы форма была готова к вводу.
  useEffect(() => {
    if (!form.kind && kinds.length) setForm((f) => ({ ...f, kind: kinds[0].id }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kinds.length]);

  function add() {
    if (!form.amount) return toast(t("expenses.needAmount"), "error");
    api
      .post("/finance/expense-entries/", { ...form, amount: Number(form.amount) })
      .then(() => {
        setForm({ ...form, name: "", amount: "", note: "" });
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
        kind: editing.kind,
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

  async function del(id) {
    if (!(await confirm(t("expenses.confirmDel")))) return;
    api
      .delete(`/finance/expense-entries/${id}/`)
      .then(() => { load(); onChanged?.(); })
      .catch(() => toast(t("common.error"), "error"));
  }

  const total = rows.reduce((s, r) => s + Number(r.amount || 0), 0);
  const sumBy = (id) => rows.filter((r) => r.kind === id).reduce((s, r) => s + Number(r.amount), 0);

  const columns = [
    { key: "spent_at", label: t("expenses.date") },
    { key: "kind", label: t("expenses.category"), render: (r) => r.kind_name },
    {
      key: "name",
      label: t("fixed.forWhat"),
      render: (r) => r.name || "—",
    },
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

  if (!kinds.length) return null;

  const kindSelect = (value, onChange) => (
    <select value={value} onChange={(e) => onChange(Number(e.target.value))}>
      {kinds.map((k) => (
        <option key={k.id} value={k.id}>{k.name}</option>
      ))}
    </select>
  );

  return (
    <>
      <h2 style={{ marginTop: 28 }}>{title}</h2>
      {subtitle && <p className="muted">{subtitle}</p>}

      <div className="card" style={{ margin: "12px 0 16px" }}>
        <div className="row">
          <div className="field" style={{ minWidth: 190 }}>
            <label>{t("expenses.category")}</label>
            {kindSelect(form.kind, (v) => setForm({ ...form, kind: v }))}
          </div>
          <div className="field grow">
            <label>{isSalary(form.kind) ? t("salary.employee") : t("fixed.forWhat")}</label>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={isSalary(form.kind) ? t("salary.employeePh") : t("fixed.forWhatPh")}
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
        <div className="field" style={{ marginTop: 4, marginBottom: 0 }}>
          <label>{t("expenses.note")}</label>
          <input
            value={form.note}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
            placeholder={t("expenses.notePh")}
          />
        </div>
      </div>

      <div className="stat-grid" style={{ marginBottom: 16 }}>
        <Stat label={t("fixed.totalForPeriod")} value={som(total)} />
        {kinds.map((k) => (
          <Stat key={k.id} label={k.name} value={som(sumBy(k.id))} />
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
            {kindSelect(editing.kind, (v) => setEditing({ ...editing, kind: v }))}
          </div>
          <div className="field">
            <label>{isSalary(editing.kind) ? t("salary.employee") : t("fixed.forWhat")}</label>
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
