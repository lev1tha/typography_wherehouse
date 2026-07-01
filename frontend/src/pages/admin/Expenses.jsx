import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const CATS = ["CUTTER", "EQUIPMENT", "IMPROVEMENT", "OTHER"];
const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

function Stat({ label, value }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

export default function Expenses({ embedded = false }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [form, setForm] = useState({ category: "CUTTER", name: "", amount: "" });

  function load() {
    api.get("/finance/expenses/").then((r) => setRows(r.data.results || r.data));
  }
  useEffect(load, []);

  function add() {
    if (!form.amount) return toast(t("expenses.needAmount"), "error");
    api
      .post("/finance/expenses/", {
        category: form.category,
        name: form.name,
        amount: Number(form.amount),
      })
      .then(() => {
        setForm({ category: form.category, name: "", amount: "" });
        load();
        toast(t("expenses.added"));
      })
      .catch(() => toast(t("common.error"), "error"));
  }

  async function del(id) {
    if (!(await confirm(t("expenses.confirmDel")))) return;
    api.delete(`/finance/expenses/${id}/`).then(load);
  }

  const sumBy = (c) => rows.filter((r) => r.category === c).reduce((s, r) => s + Number(r.amount), 0);
  const total = rows.reduce((s, r) => s + Number(r.amount), 0);

  const columns = [
    { key: "spent_at", label: t("expenses.date") },
    { key: "category", label: t("expenses.category"), render: (r) => t(`expenseCat.${r.category}`) },
    { key: "name", label: t("expenses.detailLabel"), render: (r) => r.name || "—" },
    { key: "amount", label: t("expenses.amount"), render: (r) => som(r.amount) },
    {
      key: "actions",
      label: "",
      render: (r) => (
        <button className="ghost" onClick={() => del(r.id)} aria-label={t("common.delete")}>
          <Icon name="trash" size={16} />
        </button>
      ),
    },
  ];

  return (
    <>
      {!embedded && <h1>{t("expenses.title")}</h1>}
      <p className="muted">{t("expenses.subtitle")}</p>
      <div className="card" style={{ margin: "12px 0 16px" }}>
        <div className="row">
          <div className="field" style={{ minWidth: 190 }}>
            <label>{t("expenses.category")}</label>
            <select value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })}>
              {CATS.map((c) => (
                <option key={c} value={c}>
                  {t(`expenseCat.${c}`)}
                </option>
              ))}
            </select>
          </div>
          <div className="field grow">
            <label>{t("expenses.detailLabel")}</label>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={t("expenses.detailPh")}
            />
          </div>
          <div className="field" style={{ width: 160 }}>
            <label>{t("expenses.amount")}</label>
            <input
              type="number"
              value={form.amount}
              onChange={(e) => setForm({ ...form, amount: e.target.value })}
            />
          </div>
          <div className="field" style={{ display: "flex", alignItems: "flex-end" }}>
            <button onClick={add}>{t("common.add")}</button>
          </div>
        </div>
      </div>

      <div className="stat-grid" style={{ marginBottom: 16 }}>
        <Stat label={t("expenses.totalInvest")} value={som(total)} />
        <Stat label={t("expenseCat.CUTTER")} value={som(sumBy("CUTTER"))} />
        <Stat label={t("expenseCat.EQUIPMENT")} value={som(sumBy("EQUIPMENT"))} />
        <Stat label={t("expenseCat.IMPROVEMENT")} value={som(sumBy("IMPROVEMENT"))} />
      </div>

      <DataTable columns={columns} rows={rows} />
    </>
  );
}
