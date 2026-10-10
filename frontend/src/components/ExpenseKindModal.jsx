import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useAuth } from "../auth/AuthContext.jsx";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney } from "../utils/format.js";
import Field, { focusFirstInvalid } from "./Field.jsx";

const som = (n) => formatMoney(n);
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
// «За какой месяц» пуст — сервер ставит месяц оплаты. Отправляем поле, только
// если его заполнили, иначе правка дня оплаты не потянула бы месяц следом.
const monthOf = (day) => (day || "").slice(0, 7);

export default function ExpenseKindModal({ kind, period, settings, onClose, onChanged, onEditKind }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  // Бухгалтер сюда заходит смотреть: запись в финансах сервер ему запрещает.
  const { isAccountant: readOnly } = useAuth();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  // «Чем заплатили» нужно кассовой книге: трата уходит в неё расходом, и без
  // счёта остаток наличных считал бы и переводы тоже.
  const [form, setForm] = useState({
    name: "", amount: "", spent_at: defaultDate(period), note: "", account: "CASH",
    period: "", useful_life_months: "",
  });
  // Покупка в «Инвестициях»: от порога — актив с амортизацией (срок службы,
  // месяц выбытия), дешевле — сразу расход. Решает сервер по порогу.
  const isCapex = kind.role === "CAPEX";
  const accrued = kind.basis === "accrued";
  const threshold = settings?.capitalization_threshold;
  const [editing, setEditing] = useState(null);
  // Ошибки ввода суммы — рядом с полем (форма добавления и форма правки).
  const [addErr, setAddErr] = useState("");
  const [editErr, setEditErr] = useState("");

  // У зарплат в это поле пишется имя сотрудника — мастера и резчики не заводятся
  // как пользователи системы.
  const isSalary = kind.code === "SALARY";
  const nameLabel = isSalary ? t("salary.employee") : t("fixed.forWhat");
  const namePlaceholder = isSalary ? t("salary.employeePh") : t("fixed.forWhatPh");

  function load() {
    setLoading(true);
    api
      // Расход и проценты в «Сводке» считаются по «за какой месяц» — список
      // берём тем же отбором, иначе итог окна разошёлся бы со строкой.
      .get("/finance/expense-entries/", {
        params: { kind: kind.id, ...(period || {}), ...(accrued ? { basis: "accrued" } : {}) },
      })
      .then((r) => setRows(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"))
      .finally(() => setLoading(false));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [kind.id, period?.date_from, period?.date_to]);

  function add() {
    if (!form.amount) {
      setAddErr(t("expenses.needAmount"));
      return focusFirstInvalid();
    }
    setAddErr("");
    api
      .post("/finance/expense-entries/", {
        kind: kind.id,
        name: form.name,
        amount: Number(form.amount),
        spent_at: form.spent_at,
        note: form.note,
        account: form.account,
        ...(form.period ? { period: form.period } : {}),
        ...(isCapex && form.useful_life_months ? { useful_life_months: Number(form.useful_life_months) } : {}),
      })
      .then(() => {
        setForm({
          name: "", amount: "", spent_at: form.spent_at, note: "", account: form.account,
          period: form.period, useful_life_months: "",
        });
        load();
        onChanged?.();
        toast(t("expenses.added"));
      })
      // Текст сервера, а не «ошибка»: замок периода, срок больше аренды и
      // прочие отказы объясняют, что поправить.
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }

  function saveEdit() {
    if (!editing.amount) {
      setEditErr(t("expenses.needAmount"));
      return focusFirstInvalid();
    }
    setEditErr("");
    const original = rows.find((r) => r.id === editing.id);
    api
      .patch(`/finance/expense-entries/${editing.id}/`, {
        name: editing.name,
        amount: Number(editing.amount),
        spent_at: editing.spent_at,
        note: editing.note || "",
        account: editing.account,
        // Месяц отправляем, только если его меняли: иначе правка дня оплаты
        // не потянула бы за собой месяц, стоявший по умолчанию.
        ...(editing.period !== original?.period ? { period: editing.period || null } : {}),
        ...(editing.is_capitalized
          ? {
              useful_life_months: Number(editing.useful_life_months) || null,
              depreciate_until: editing.depreciate_until || null,
            }
          : {}),
      })
      .then(() => {
        setEditing(null);
        load();
        onChanged?.();
        toast(t("common.saved"));
      })
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
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
        {isCapex && ` · ${t("kinds.capexRoleHint")}`}
        {period?.date_from && ` · ${accrued ? t("kinds.accruedBasis") : t("kinds.paidBasis")}`}
      </p>

      {/* Закуп материала руками не вносится: система считает его по приходам,
          а ручная запись ложилась в «Закуп» второй раз. Оплата поставщику —
          в «Долге поставщикам». */}
      {!readOnly && kind.code === "MATERIAL_PURCHASE" && (
        <p className="muted" style={{ fontSize: 13 }}>{t("kinds.purchaseIsAuto")}</p>
      )}
      {!readOnly && kind.code !== "MATERIAL_PURCHASE" && (
      <div className="card" style={{ margin: "10px 0 14px", background: "var(--primary-soft)" }}>
        <div className="row">
          <Field className="grow" label={nameLabel}>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={namePlaceholder}
            />
          </Field>
          <Field style={{ width: 150 }} label={t("expenses.date")}>
            <input
              type="date"
              value={form.spent_at}
              onChange={(e) => setForm({ ...form, spent_at: e.target.value })}
            />
          </Field>
          <Field style={{ width: 130 }} label={t("expenses.amount")} required error={addErr}>
            <input
              type="number"
              inputMode="decimal"
              value={form.amount}
              onChange={(e) => { setForm({ ...form, amount: e.target.value }); setAddErr(""); }}
              onKeyDown={(e) => e.key === "Enter" && add()}
            />
          </Field>
          <Field style={{ width: 130 }} label={t("expenses.paidFrom")}>
            <select
              value={form.account}
              onChange={(e) => setForm({ ...form, account: e.target.value })}
            >
              <option value="CASH">{t("expenses.paidCash")}</option>
              <option value="BANK">{t("expenses.paidBank")}</option>
            </select>
          </Field>
          <div className="field" style={{ display: "flex", alignItems: "flex-end" }}>
            <button onClick={add}>{t("common.add")}</button>
          </div>
        </div>
        <div className="row" style={{ marginTop: 2 }}>
          <Field style={{ width: 170, marginBottom: 0 }} label={t("expenses.period")}>
            <input
              type="month"
              value={form.period}
              placeholder={monthOf(form.spent_at)}
              onChange={(e) => setForm({ ...form, period: e.target.value })}
            />
          </Field>
          {isCapex && (
            <Field style={{ width: 170, marginBottom: 0 }} label={t("expenses.usefulLife")}>
              <input
                type="number"
                min="1"
                value={form.useful_life_months}
                placeholder="60"
                onChange={(e) => setForm({ ...form, useful_life_months: e.target.value })}
              />
            </Field>
          )}
        </div>
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("expenses.periodHint")}</p>
        {isCapex && (
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
            {threshold != null
              ? t("expenses.capexHint", { threshold: som(threshold) })
              : t("expenses.capexHintNoValue")}
          </p>
        )}
        <Field style={{ marginTop: 8, marginBottom: 0 }} label={t("expenses.note")}>
          <input
            value={form.note}
            onChange={(e) => setForm({ ...form, note: e.target.value })}
            placeholder={t("expenses.notePh")}
          />
        </Field>
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
                  <Field className="grow" label={nameLabel}>
                    <input
                      value={editing.name || ""}
                      onChange={(e) => setEditing({ ...editing, name: e.target.value })}
                    />
                  </Field>
                  <Field style={{ width: 150 }} label={t("expenses.date")}>
                    <input
                      type="date"
                      value={editing.spent_at}
                      onChange={(e) => setEditing({ ...editing, spent_at: e.target.value })}
                    />
                  </Field>
                  <Field style={{ width: 130 }} label={t("expenses.amount")} required error={editErr}>
                    <input
                      type="number"
                      inputMode="decimal"
                      value={editing.amount}
                      onChange={(e) => { setEditing({ ...editing, amount: e.target.value }); setEditErr(""); }}
                    />
                  </Field>
                  <Field style={{ width: 130 }} label={t("expenses.paidFrom")}>
                    <select
                      value={editing.account || "CASH"}
                      onChange={(e) => setEditing({ ...editing, account: e.target.value })}
                    >
                      <option value="CASH">{t("expenses.paidCash")}</option>
                      <option value="BANK">{t("expenses.paidBank")}</option>
                    </select>
                  </Field>
                </div>
                <div className="row">
                  <Field style={{ width: 170 }} label={t("expenses.period")}>
                    <input
                      type="month"
                      value={editing.period || ""}
                      onChange={(e) => setEditing({ ...editing, period: e.target.value })}
                    />
                  </Field>
                  {editing.is_capitalized && (
                    <>
                      <Field style={{ width: 150 }} label={t("expenses.usefulLife")}>
                        <input
                          type="number"
                          min="1"
                          value={editing.useful_life_months || ""}
                          onChange={(e) => setEditing({ ...editing, useful_life_months: e.target.value })}
                        />
                      </Field>
                      <Field style={{ width: 190 }} label={t("expenses.depreciateUntil")}>
                        <input
                          type="month"
                          value={editing.depreciate_until || ""}
                          onChange={(e) => setEditing({ ...editing, depreciate_until: e.target.value })}
                        />
                      </Field>
                    </>
                  )}
                </div>
                {editing.is_capitalized && (
                  <p className="muted" style={{ fontSize: 12, margin: "-4px 0 8px" }}>
                    {t("expenses.depreciateUntilHint")}
                  </p>
                )}
                <Field label={t("expenses.note")}>
                  <input
                    value={editing.note || ""}
                    onChange={(e) => setEditing({ ...editing, note: e.target.value })}
                  />
                </Field>
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
                  {r.period && r.period !== monthOf(r.spent_at) && (
                    <span className="muted" style={{ fontSize: 12 }}> · {t("expenses.forMonth", { month: r.period })}</span>
                  )}
                  {r.name && <> · <strong>{r.name}</strong></>}
                  {isCapex && (
                    <div className="muted" style={{ fontSize: 12 }}>
                      {r.is_capitalized
                        ? t("expenses.assetBadge", { months: r.useful_life_months })
                        : t("expenses.belowThresholdBadge")}
                      {r.depreciate_until && ` · ${t("expenses.disposedBadge", { month: r.depreciate_until })}`}
                    </div>
                  )}
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
        <strong style={{ color: "var(--accent-ink)" }}>{t("fixed.totalForPeriod")}</strong>
        <strong style={{ color: "var(--accent-ink)" }}>{som(total)}</strong>
      </div>
    </Modal>
  );
}
