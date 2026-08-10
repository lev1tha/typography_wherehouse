import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import DataTable from "./DataTable.jsx";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

function Stat({ label, value }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}

// Все траты за период ОДНОЙ таблицей: когда, на что, сколько и в какую строку
// отчёта попало.
//
// Раньше здесь было три отдельные секции — «Постоянные расходы», «Зарплаты»,
// «Покупки», — и у каждой свой ряд плиток с суммами по видам. Те же виды с теми
// же суммами уже стоят в блоках отчёта выше, поэтому экран заканчивался тремя
// экранами нулей подряд: «Аренда 0 · Коммуналка 0 · Интернет 0 · Нет данных»,
// и так трижды. Плитки убраны как дубль, таблицы слиты в одну.
//
// ДОБАВЛЕНИЯ здесь тоже нет: форма «вид расхода · за что · дата · сумма»
// повторяла диалог строки отчёта, только хуже — вид приходилось выбирать
// руками, и не было видно, куда трата попадёт. Правка и удаление остались.
export default function ExpenseListSection({ title, subtitle, kinds, period, onChanged }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [editing, setEditing] = useState(null);

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
      {title && <h3 style={{ marginTop: 22 }}>{title}</h3>}
      {subtitle && <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{subtitle}</p>}

      {/* Итог одной строкой, а не рядом плиток по видам: суммы по видам стоят
          в блоках отчёта выше, и повторять их здесь незачем. */}
      {rows.length > 0 && (
        <div className="crow" style={{ marginBottom: 6 }}>
          <span className="k">{t("fixed.totalForPeriod")}</span>
          <strong>{som(total)}</strong>
        </div>
      )}
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
