import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import DataTable from "./DataTable.jsx";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import PaySupplierModal from "./PaySupplierModal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatDate, formatMoney, formatNumber } from "../utils/format.js";

const som = (n) => formatMoney(n);
const today = () => new Date().toLocaleDateString("sv-SE");

// Сальдо поставщика одним цветом и словом: плюс — мы должны, минус — деньги
// лежат у поставщика (аванс или переплата). Цифры на экране и в CSV — одни.
function Saldo({ value, t }) {
  const v = Number(value) || 0;
  if (Math.abs(v) < 0.005) return <span className="badge ok">{t("suppliers.zero")}</span>;
  return v > 0 ? (
    <strong style={{ color: "var(--danger-ink)" }}>{som(v)}</strong>
  ) : (
    <strong style={{ color: "var(--ok-ink)" }}>{t("suppliers.advanceOf", { sum: som(-v) })}</strong>
  );
}

export default function SuppliersPanel({ canWrite, onOpenSupply, onChanged }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [rows, setRows] = useState([]);
  const [q, setQ] = useState("");
  const [card, setCard] = useState(null);

  function load() {
    api.get("/warehouse/suppliers/")
      .then((r) => setRows(r.data.results || r.data))
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, []);

  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    return s ? rows.filter((r) => `${r.name} ${r.phone || ""} ${r.inn || ""}`.toLowerCase().includes(s)) : rows;
  }, [rows, q]);
  const totalOwe = rows.reduce((s, r) => s + Math.max(0, Number(r.balance?.saldo) || 0), 0);
  const totalAdv = rows.reduce((s, r) => s + Math.max(0, -(Number(r.balance?.saldo) || 0)), 0);

  const columns = [
    { key: "name", label: t("supplies.supplier"), render: (r) => <strong>{r.name}</strong> },
    { key: "phone", label: t("suppliers.phone"), render: (r) => r.phone || <span className="muted">—</span> },
    { key: "supplies_count", label: t("suppliers.docs"), render: (r) => r.supplies_count },
    {
      key: "balance",
      label: t("suppliers.saldo"),
      render: (r) => (
        <>
          <Saldo value={r.balance?.saldo} t={t} />
          {Object.entries(r.balance?.foreign_debts || {}).map(([cur, sum]) => (
            <div key={cur} className="muted" style={{ fontSize: 12 }}>{formatNumber(sum, { max: 2 })} {cur}</div>
          ))}
        </>
      ),
    },
    {
      key: "open",
      label: "",
      render: (r) => (
        <button className="secondary row-btn" onClick={() => setCard(r)}>{t("suppliers.openCard")}</button>
      ),
    },
  ];

  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0, maxWidth: "70ch" }}>{t("suppliers.hint")}</p>
      <div className="stat-grid" style={{ margin: "14px 0" }}>
        <div className="stat">
          <div className="label">{t("suppliers.statOwe")}</div>
          <div className="value" style={totalOwe > 0 ? { color: "var(--danger-ink)" } : undefined}>{som(totalOwe)}</div>
        </div>
        <div className="stat">
          <div className="label">{t("suppliers.statAdvance")}</div>
          <div className="value" style={totalAdv > 0 ? { color: "var(--ok-ink)" } : undefined}>{som(totalAdv)}</div>
        </div>
      </div>
      <input
        className="search" placeholder={t("common.search")} aria-label={t("common.search")}
        value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 320, marginBottom: 10 }}
      />
      <DataTable columns={columns} rows={shown} onRowClick={setCard} filtered={!!q && !shown.length} onReset={() => setQ("")} />
      {card && (
        <SupplierCard
          supplier={card}
          canWrite={canWrite}
          onClose={() => setCard(null)}
          onOpenSupply={(id) => { setCard(null); onOpenSupply?.(id); }}
          onChanged={() => { load(); onChanged?.(); }}
        />
      )}
    </>
  );
}

function SupplierCard({ supplier, canWrite, onClose, onOpenSupply, onChanged }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [data, setData] = useState(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [paying, setPaying] = useState(false);
  const [debtOpen, setDebtOpen] = useState(false);
  const [applying, setApplying] = useState(null);

  function load() {
    api.get(`/warehouse/suppliers/${supplier.id}/statement/`, {
      params: { ...(from ? { date_from: from } : {}), ...(to ? { date_to: to } : {}) },
    })
      .then((r) => setData(r.data))
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [from, to]);

  async function csv() {
    try {
      const r = await api.get(`/warehouse/suppliers/${supplier.id}/statement/`, {
        params: { export: "csv", ...(from ? { date_from: from } : {}), ...(to ? { date_to: to } : {}) },
        responseType: "blob",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(r.data);
      a.download = `postavshchik-${supplier.id}-vypiska.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function removeOpening(d) {
    if (!(await confirm(t("suppliers.openingDelConfirm", { sum: som(d.amount), date: formatDate(d.as_of) })))) return;
    try {
      await api.delete(`/warehouse/supplier-opening-debts/${d.id}/`);
      load();
      onChanged?.();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const b = data?.balance;
  const typeLabel = (r) => t(`suppliers.type.${r.type}`, { defaultValue: r.type });

  return (
    <>
    <Modal wide title={`${t("suppliers.card")} · ${supplier.name}`} onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={csv}>{t("finance.downloadCsv")}</button>
          {canWrite && <button className="secondary" onClick={() => setDebtOpen(true)}>{t("suppliers.addOpening")}</button>}
          {canWrite && <button onClick={() => setPaying(true)}>{t("suppliers.advanceBtn")}</button>}
        </>
      }
    >
      {!data ? (
        <p className="muted">{t("common.loading")}</p>
      ) : (
        <>
          <div className="stat-grid" style={{ margin: "0 0 12px" }}>
            <div className="stat">
              <div className="label">{t("suppliers.saldo")}</div>
              <div className="value"><Saldo value={b.saldo} t={t} /></div>
            </div>
            <div className="stat"><div className="label">{t("suppliers.invoices")}</div><div className="value">{som(b.invoices)}</div></div>
            <div className="stat"><div className="label">{t("suppliers.paid")}</div><div className="value">{som(b.paid)}</div></div>
            {Number(b.opening) !== 0 && (
              <div className="stat"><div className="label">{t("suppliers.opening")}</div><div className="value">{som(b.opening)}</div></div>
            )}
          </div>
          {Object.keys(b.foreign_debts || {}).length > 0 && (
            <p style={{ fontSize: 13, margin: "0 0 10px" }}>
              {t("suppliers.foreignDebt")}: {Object.entries(b.foreign_debts).map(([cur, sum]) => `${formatNumber(sum, { max: 2 })} ${cur}`).join(", ")}
            </p>
          )}

          {(data.opening_debts || []).map((d) => (
            <div key={d.id} className="crow">
              <span className="k">{t("suppliers.openingRow", { date: formatDate(d.as_of) })}{d.note ? ` · ${d.note}` : ""}</span>
              <span>
                {som(d.amount)}{" "}
                {canWrite && (
                  <button className="ghost row-danger" aria-label={t("common.delete")} onClick={() => removeOpening(d)}>×</button>
                )}
              </span>
            </div>
          ))}
          {(data.advances || []).filter((a) => Number(a.advance_left) > 0).map((a) => (
            <div key={a.id} className="crow">
              <span className="k">{t("suppliers.advanceRow", { date: formatDate(a.paid_on) })}{a.note ? ` · ${a.note}` : ""}</span>
              <span>
                {t("suppliers.advanceLeft", { left: som(a.advance_left), sum: som(a.amount) })}{" "}
                {canWrite && (
                  <button className="secondary row-btn" onClick={() => setApplying(a)}>{t("suppliers.applyAdvance")}</button>
                )}
              </span>
            </div>
          ))}

          <div className="row" style={{ margin: "12px 0 6px", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
            <Field style={{ margin: 0, width: 160 }} label={t("suppliers.from")}>
              <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
            </Field>
            <Field style={{ margin: 0, width: 160 }} label={t("suppliers.to")}>
              <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
            </Field>
            {(from || to) && (
              <span className="muted" style={{ fontSize: 13 }}>
                {t("suppliers.openingBalance")}: <strong>{som(data.opening_balance)}</strong>
              </span>
            )}
          </div>

          <div className="table-scroll">
            <table className="table plain-table">
              <thead>
                <tr>
                  <th>{t("supplies.date")}</th>
                  <th>{t("suppliers.doc")}</th>
                  <th style={{ textAlign: "right" }}>{t("suppliers.delta")}</th>
                  <th style={{ textAlign: "right" }}>{t("suppliers.balance")}</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <tr key={`${r.type}-${r.id}`}>
                    <td style={{ whiteSpace: "nowrap" }}>{formatDate(r.date)}</td>
                    <td>
                      {r.supply && (r.type === "INVOICE" || r.type === "RETURN") ? (
                        <button className="ghost" style={{ padding: 0, textAlign: "left" }} onClick={() => onOpenSupply(r.supply)}>
                          {r.doc}
                        </button>
                      ) : r.doc}
                      <div className="muted" style={{ fontSize: 12 }}>
                        {typeLabel(r)}
                        {r.account ? ` · ${t(`suppliersDebt.${r.account === "CASH" ? "cash" : "bank"}`)}` : ""}
                        {r.amount_fc != null ? ` · ${formatNumber(r.amount_fc, { max: 2 })} ${r.currency} ${t("supplies.atRate", { rate: formatNumber(r.rate, { max: 4 }) })}` : ""}
                        {Number(r.fx_diff) ? ` · ${t("supplies.fxDiff")} ${formatNumber(r.fx_diff, { max: 2 })}` : ""}
                        {r.note ? ` · ${r.note}` : ""}
                      </div>
                    </td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap", color: Number(r.delta) < 0 ? "var(--ok-ink)" : undefined }}>
                      {Number(r.delta) > 0 ? "+" : ""}{formatNumber(r.delta, { max: 2 })}
                    </td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>{formatNumber(r.balance, { max: 2 })}</td>
                  </tr>
                ))}
                {!data.rows.length && (
                  <tr><td colSpan={4} className="muted">{t("common.empty")}</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Modal>

      {paying && (
        <PaySupplierModal
          row={{ kind: "ADVANCE", supplier_id: supplier.id, label: t("suppliers.advanceTitle"), supplier: supplier.name }}
          onClose={() => setPaying(false)}
          onPaid={() => { setPaying(false); load(); onChanged?.(); }}
        />
      )}
      {debtOpen && (
        <OpeningDebtModal
          supplier={supplier}
          onClose={() => setDebtOpen(false)}
          onDone={() => { setDebtOpen(false); load(); onChanged?.(); }}
        />
      )}
      {applying && (
        <ApplyAdvanceModal
          supplier={supplier}
          advance={applying}
          onClose={() => setApplying(null)}
          onDone={() => { setApplying(null); load(); onChanged?.(); }}
        />
      )}
    </>
  );
}

function OpeningDebtModal({ supplier, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [amount, setAmount] = useState("");
  const [asOf, setAsOf] = useState(today());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const valid = Number(amount) !== 0 && !Number.isNaN(Number(amount)) && amount !== "";

  async function submit() {
    setBusy(true);
    try {
      await api.post("/warehouse/supplier-opening-debts/", {
        supplier: supplier.id, amount: Number(amount), as_of: asOf, note,
      });
      toast(t("suppliers.openingSaved"));
      onDone();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("suppliers.addOpening")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("common.save")}</button>
        </>
      }
    >
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>{t("suppliers.openingHint")}</p>
      <Field label={t("suppliers.openingAmount")}>
        <input type="number" step="any" inputMode="decimal" autoFocus value={amount} onChange={(e) => setAmount(e.target.value)} />
      </Field>
      <div className="row">
        <Field className="grow" style={{ margin: 0 }} label={t("suppliers.openingDate")}>
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} />
        </Field>
        <Field className="grow" style={{ margin: 0 }} label={t("supplies.note")}>
          <input value={note} maxLength={255} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}

function ApplyAdvanceModal({ supplier, advance, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [supplies, setSupplies] = useState([]);
  const [supply, setSupply] = useState("");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get("/warehouse/supplies/", { params: { supplier: supplier.id, unpaid: 1, page_size: 100 } })
      .then((r) => setSupplies((r.data.results || r.data).filter((s) => Number(s.debt) > 0 && s.currency === "KGS")))
      .catch(() => {});
  }, [supplier.id]);

  const chosen = supplies.find((s) => String(s.id) === String(supply));
  const max = chosen ? Math.min(Number(advance.advance_left), Number(chosen.debt)) : Number(advance.advance_left);
  const valid = !!chosen && Number(amount) > 0 && Number(amount) <= max + 1e-9;

  async function submit() {
    setBusy(true);
    try {
      await api.post(`/warehouse/supplier-payments/${advance.id}/apply/`, { supply: Number(supply), amount: Number(amount) });
      toast(t("suppliers.applied"));
      onDone();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("suppliers.applyAdvance")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("suppliers.applyDo")}</button>
        </>
      }
    >
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
        {t("suppliers.applyHint", { left: som(advance.advance_left) })}
      </p>
      <Field label={t("suppliers.applyTo")}>
        <select value={supply} onChange={(e) => { setSupply(e.target.value); const s = supplies.find((x) => String(x.id) === e.target.value); if (s) setAmount(String(Math.min(Number(advance.advance_left), Number(s.debt)))); }}>
          <option value="" disabled>{t("suppliersDebt.chooseAccount")}</option>
          {supplies.map((s) => (
            <option key={s.id} value={s.id}>
              {(s.number || `#${s.id}`)} · {formatDate(s.received_on)} · {t("supplies.debt")} {som(s.debt)}
            </option>
          ))}
        </select>
      </Field>
      <Field label={t("receipts.payAmount")}>
        <input type="number" min="0" step="any" max={max} value={amount} onChange={(e) => setAmount(e.target.value)} />
      </Field>
    </Modal>
  );
}
