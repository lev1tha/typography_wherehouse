import { useTranslation } from "react-i18next";

import Modal from "./Modal.jsx";
import { formatDate, formatMoney } from "../utils/format.js";

const som = (n) => formatMoney(n);
const ru = (iso) => formatDate(iso);

// Долг поставщикам — ДОКУМЕНТАМИ, а не одной цифрой. Владелец спросил «за что
// должны?»: строка «Накладная НК-1 · 22 550» без содержимого ничего не
// доказывает. Здесь у каждого долга видно, что это за документ, от кого и
// когда, что именно пришло и почём, сколько уже заплачено.
export default function SupplierDebtModal({ debts, readOnly, onClose, onPay }) {
  const { t } = useTranslation();
  const rows = debts?.rows || [];
  return (
    <Modal
      title={t("suppliersDebt.title")}
      onClose={onClose}
      wide
      footer={<button onClick={onClose}>{t("common.close")}</button>}
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -4 }}>{t("suppliersDebt.hint")}</p>
      {rows.map((r) => (
        <div key={`${r.kind}-${r.id}`} className="card" style={{ padding: 12, margin: "10px 0", background: "var(--canvas)" }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", gap: 10, margin: 0 }}>
            <div>
              <strong>{r.label}</strong>
              <div className="muted" style={{ fontSize: 12 }}>
                {ru(r.date)}
                {r.supplier ? ` · ${r.supplier}` : ""}
                {r.created_by ? ` · ${t("suppliersDebt.acceptedBy", { name: r.created_by })}` : ""}
              </div>
            </div>
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <strong style={{ color: "var(--danger-ink)" }}>{som(r.debt)}</strong>
              {!readOnly && (
                <button className="secondary" onClick={() => onPay(r)}>{t("suppliersDebt.pay")}</button>
              )}
            </div>
          </div>
          {/* Что пришло — позиции документа. */}
          <table style={{ marginTop: 8, fontSize: 13 }}>
            <tbody>
              {(r.lines || []).map((l, i) => (
                <tr key={i}>
                  <td>{l.material}</td>
                  <td className="muted">{l.what}</td>
                  <td><span className="sheet-num">{som(l.cost)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            {t("suppliersDebt.docMoney", { total: som(r.total), paid: som(r.paid) })}
            {r.note ? ` · ${r.note}` : ""}
          </div>
        </div>
      ))}
      <div className="crow" style={{ marginTop: 6 }}>
        <strong>{t("suppliersDebt.total")}</strong>
        <strong style={{ color: "var(--danger-ink)" }}>{som(debts?.total || 0)}</strong>
      </div>
    </Modal>
  );
}
