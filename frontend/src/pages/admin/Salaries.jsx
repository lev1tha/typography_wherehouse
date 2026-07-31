import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const today = () => new Date().toISOString().slice(0, 10);

// Зарплата вынесена в отдельный раздел и ведётся по людям: видно, кому и
// сколько выплатили за месяц, а не одну общую сумму.
export default function Salaries({ embedded = false }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [period, setPeriod] = useState({ year: new Date().getFullYear(), month: null });
  const [form, setForm] = useState({ employee: "", amount: "", paid_at: today(), note: "" });
  const [editing, setEditing] = useState(null);

  function load() {
    const params = period.month ? { year: period.year, month: period.month } : {};
    api
      .get("/finance/salaries/", { params })
      .then((r) => setRows(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [period.year, period.month]);

  function add() {
    if (!form.employee.trim()) return toast(t("salary.needEmployee"), "error");
    if (!form.amount) return toast(t("expenses.needAmount"), "error");
    api
      .post("/finance/salaries/", { ...form, amount: Number(form.amount) })
      .then(() => {
        setForm({ employee: "", amount: "", paid_at: form.paid_at, note: "" });
        load();
        toast(t("salary.added"));
      })
      .catch(() => toast(t("common.error"), "error"));
  }

  function saveEdit() {
    if (!editing.amount) return toast(t("expenses.needAmount"), "error");
    api
      .patch(`/finance/salaries/${editing.id}/`, {
        employee: editing.employee,
        amount: Number(editing.amount),
        paid_at: editing.paid_at,
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
    api.delete(`/finance/salaries/${id}/`).then(load);
  }

  const total = rows.reduce((s, r) => s + Number(r.amount), 0);

  // Сводка «кому сколько» за выбранный период — главное, ради чего разделяли.
  const byEmployee = useMemo(() => {
    const map = {};
    for (const r of rows) map[r.employee] = (map[r.employee] || 0) + Number(r.amount);
    return Object.entries(map).sort((a, b) => b[1] - a[1]);
  }, [rows]);

  // Подсказка с уже встречавшимися именами — чтобы не печатать заново.
  const knownEmployees = useMemo(() => [...new Set(rows.map((r) => r.employee))], [rows]);

  const columns = [
    { key: "paid_at", label: t("expenses.date") },
    { key: "employee", label: t("salary.employee"), render: (r) => <strong>{r.employee}</strong> },
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
      {!embedded && <h1>{t("salary.title")}</h1>}
      <p className="muted">{t("salary.subtitle")}</p>

      <div className="card" style={{ margin: "12px 0 16px" }}>
        <div className="row">
          <div className="field grow">
            <label>{t("salary.employee")}</label>
            <input
              value={form.employee}
              onChange={(e) => setForm({ ...form, employee: e.target.value })}
              placeholder={t("salary.employeePh")}
              list="known-employees"
            />
            <datalist id="known-employees">
              {knownEmployees.map((n) => (
                <option key={n} value={n} />
              ))}
            </datalist>
          </div>
          <div className="field" style={{ width: 160 }}>
            <label>{t("expenses.date")}</label>
            <input type="date" value={form.paid_at} onChange={(e) => setForm({ ...form, paid_at: e.target.value })} />
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
            placeholder={t("salary.notePh")}
          />
        </div>
      </div>

      <div className="toolbar" style={{ alignItems: "center", marginBottom: 12 }}>
        <MonthPicker value={period} onChange={setPeriod} />
      </div>

      <div className="stat-grid" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="label">{t("salary.totalForPeriod")}</div>
          <div className="value">{som(total)}</div>
        </div>
        {byEmployee.map(([name, sum]) => (
          <div className="stat" key={name}>
            <div className="label">{name}</div>
            <div className="value">{som(sum)}</div>
          </div>
        ))}
      </div>

      <DataTable columns={columns} rows={rows} />

      {editing && (
        <Modal
          title={t("salary.editTitle")}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setEditing(null)}>{t("common.cancel")}</button>
              <button onClick={saveEdit}>{t("common.save")}</button>
            </>
          }
        >
          <div className="field">
            <label>{t("salary.employee")}</label>
            <input value={editing.employee} onChange={(e) => setEditing({ ...editing, employee: e.target.value })} />
          </div>
          <div className="field">
            <label>{t("expenses.date")}</label>
            <input type="date" value={editing.paid_at} onChange={(e) => setEditing({ ...editing, paid_at: e.target.value })} />
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
