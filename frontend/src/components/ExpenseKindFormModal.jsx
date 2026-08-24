import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Свой вид расхода можно завести в любом блоке. В «Материалах» итог считается
// как остаток на начало + Σ(строки с «уменьшает прибыль») − остаток на конец,
// поэтому лишняя строка формулу не ломает: со снятым флагом она останется
// справочной, как «долг материала».
const USER_BLOCKS = ["MATERIALS", "FIXED", "VARIABLE", "INVESTMENT"];
const BLOCK_LABEL = {
  MATERIALS: "blockMaterials",
  FIXED: "blockFixed",
  VARIABLE: "blockVariable",
  INVESTMENT: "blockInvestment",
};

// Создание и правка вида расхода — строки финотчёта. Раньше виды были зашиты в
// код, и завести «Рекламу» или «Налоги» без правки исходников было нельзя.
export default function ExpenseKindFormModal({ kind, block, onClose, onSaved }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const isNew = !kind;
  const [form, setForm] = useState({
    name: kind?.name || "",
    block: kind?.block || block || "FIXED",
    in_profit: kind?.in_profit ?? true,
  });
  const [busy, setBusy] = useState(false);

  function save() {
    if (!form.name.trim()) return toast(t("kinds.needName"), "error");
    setBusy(true);
    const req = isNew
      ? api.post("/finance/expense-kinds/", form)
      : api.patch(`/finance/expense-kinds/${kind.id}/`, form);
    req
      .then(() => {
        toast(isNew ? t("kinds.created") : t("common.saved"));
        onSaved?.();
        onClose();
      })
      .catch((e) => toast(e.response?.data?.block?.[0] || t("common.error"), "error"))
      .finally(() => setBusy(false));
  }

  async function remove() {
    if (!(await confirm(t("kinds.confirmDelete")))) return;
    setBusy(true);
    api
      .delete(`/finance/expense-kinds/${kind.id}/`)
      .then((r) => {
        // С историей вид не удаляется, а скрывается — иначе суммы прошлых
        // месяцев поехали бы задним числом.
        toast(r.data?.archived ? t("kinds.hidden") : t("kinds.deleted"));
        onSaved?.();
        onClose();
      })
      .catch((e) => toast(e.response?.data?.detail || t("common.error"), "error"))
      .finally(() => setBusy(false));
  }

  return (
    <Modal
      title={isNew ? t("kinds.newTitle") : t("kinds.editTitle")}
      onClose={onClose}
      footer={
        <>
          {!isNew && !kind.is_builtin && (
            <button className="ghost" style={{ color: "var(--danger)" }} onClick={remove} disabled={busy}>
              {t("kinds.delete")}
            </button>
          )}
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy}>{t("common.save")}</button>
        </>
      }
    >
      <div className="field">
        <label>{t("kinds.name")}</label>
        <input
          autoFocus
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder={t("kinds.namePh")}
        />
      </div>

      <div className="field">
        <label>{t("kinds.block")}</label>
        {/* Блок встроенного вида менять нельзя: отчёт опирается на то, что
            транспорт лежит в «Материалах», а зарплаты — в «Постоянных». */}
        <select
          value={form.block}
          onChange={(e) => setForm({ ...form, block: e.target.value })}
          disabled={!isNew && kind.is_builtin}
        >
          {USER_BLOCKS.map((b) => (
            <option key={b} value={b}>{t(`finance.${BLOCK_LABEL[b]}`)}</option>
          ))}
        </select>
        {!isNew && kind.is_builtin && (
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("kinds.builtinHint")}</p>
        )}
      </div>

      {/* У инвестиций галочки нет: блок в прибыль не входит по определению,
          сервер снимает флаг сам — форма не должна обещать выбор, которого нет. */}
      {form.block !== "INVESTMENT" ? (
        <div className="field">
          <label style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={form.in_profit}
              onChange={(e) => setForm({ ...form, in_profit: e.target.checked })}
              style={{ width: 18, height: 18 }}
            />
            {t("kinds.inProfit")}
          </label>
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("kinds.inProfitHint")}</p>
        </div>
      ) : (
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("kinds.investmentHint")}</p>
      )}
    </Modal>
  );
}
