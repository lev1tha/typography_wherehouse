import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import PrintSupply from "../../components/PrintSupply.jsx";
import RefSelect from "../../components/RefSelect.jsx";
import { useUI } from "../../components/UIProvider.jsx";

// Приходные накладные — поставка целиком, одним документом.
//
// Раньше приход вводился по одной позиции с кнопки на строке материала:
// поставка на восемь позиций — восемь отдельных операций, и сверить итог с
// бумажной накладной было нечем. Здесь строки вводятся сеткой, а сумма по
// бумаге стоит рядом с суммой системы: сошлось или нет, видно сразу.

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const q2 = (n) => Number(n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
const today = () => new Date().toLocaleDateString("sv-SE");

const EMPTY_LINE = {
  material: "", form: "SHEET",
  width: "", height: "", length: "", sheet_count: "", quantity: "", cost: "", code: "",
};

/** Сколько единиц встанет на склад по строке — та же формула, что на сервере. */
function lineQuantity(line, material) {
  if (!material) return 0;
  const n = (v) => Number(v) || 0;
  if (!material.is_roll_material || line.form === "QTY") return n(line.quantity);
  if (line.form === "ROLL") return n(line.width) * n(line.length);
  return n(line.width) * n(line.height) * n(line.sheet_count);
}

export default function Supplies({ embedded = false }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin } = useAuth();
  const [rows, setRows] = useState([]);
  const [suppliers, setSuppliers] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [open, setOpen] = useState(null);   // просмотр накладной
  const [draft, setDraft] = useState(null); // новая накладная
  const [printing, setPrinting] = useState(null); // печатная форма накладной
  const [busy, setBusy] = useState(false);

  function load() {
    api.get("/warehouse/supplies/", { params: { page_size: 100 } })
      .then((r) => setRows(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"));
  }
  function loadSuppliers() {
    return api.get("/warehouse/suppliers/").then((r) => setSuppliers(r.data.results || r.data));
  }
  useEffect(() => {
    load();
    loadSuppliers();
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const matById = useMemo(
    () => Object.fromEntries(materials.map((m) => [String(m.id), m])),
    [materials]
  );

  // --- новая накладная ----------------------------------------------------
  function startDraft() {
    setDraft({
      number: "", supplier: "", received_on: today(), stated_total: "",
      paid_amount: "", note: "",
      lines: [{ ...EMPTY_LINE }],
    });
  }
  const setField = (k) => (v) => setDraft((d) => ({ ...d, [k]: v }));
  function setLine(i, patch) {
    setDraft((d) => ({
      ...d,
      lines: d.lines.map((l, idx) => (idx === i ? { ...l, ...patch } : l)),
    }));
  }
  function addLine() {
    setDraft((d) => ({ ...d, lines: [...d.lines, { ...EMPTY_LINE }] }));
  }
  function dropLine(i) {
    setDraft((d) => ({ ...d, lines: d.lines.filter((_, idx) => idx !== i) }));
  }
  // Материал сам подсказывает форму прихода: акрил листами, плёнка рулоном,
  // крепёж количеством. Складовщик её меняет только когда привезли иначе.
  function pickMaterial(i, id) {
    const m = matById[String(id)];
    const form = !m ? "SHEET" : !m.is_roll_material ? "QTY" : m.intake_form || "SHEET";
    setLine(i, {
      material: id,
      form,
      width: m?.sheet_width && form === "SHEET" ? String(m.sheet_width) : "",
      height: m?.sheet_height && form === "SHEET" ? String(m.sheet_height) : "",
    });
  }

  const filled = (draft?.lines || []).filter((l) => l.material && Number(l.cost) > 0);
  const draftTotal = filled.reduce((s, l) => s + Number(l.cost || 0), 0);
  const stated = draft?.stated_total === "" ? null : Number(draft?.stated_total);
  const diff = stated == null ? 0 : stated - draftTotal;

  async function save() {
    if (!filled.length) return toast(t("supplies.needLines"), "error");
    setBusy(true);
    try {
      const payload = {
        number: draft.number,
        supplier: draft.supplier || null,
        received_on: draft.received_on,
        stated_total: draft.stated_total === "" ? null : Number(draft.stated_total),
        paid_amount: Number(draft.paid_amount) || 0,
        note: draft.note,
        lines: filled.map((l) => ({
          material: Number(l.material),
          form: l.form,
          width: l.width === "" ? null : Number(l.width),
          height: l.height === "" ? null : Number(l.height),
          length: l.length === "" ? null : Number(l.length),
          sheet_count: l.sheet_count === "" ? null : Number(l.sheet_count),
          quantity: l.quantity === "" ? 0 : Number(l.quantity),
          cost: Number(l.cost),
          code: l.code,
        })),
      };
      await api.post("/warehouse/supplies/", payload);
      setDraft(null);
      load();
      toast(t("supplies.posted"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function cancelSupply(row) {
    if (!(await confirm(t("supplies.cancelConfirm", { n: row.number || `#${row.id}` })))) return;
    try {
      await api.delete(`/warehouse/supplies/${row.id}/`);
      setOpen(null);
      load();
      toast(t("supplies.cancelled"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const columns = [
    {
      key: "number",
      label: t("supplies.number"),
      render: (r) => (
        <>
          <strong>{r.number || `#${r.id}`}</strong>
          {r.note ? <div className="muted" style={{ fontSize: 12 }}>{r.note}</div> : null}
        </>
      ),
    },
    { key: "received_on", label: t("supplies.date"), render: (r) => new Date(r.received_on).toLocaleDateString("ru-RU") },
    { key: "supplier_name", label: t("supplies.supplier"), render: (r) => r.supplier_name || <span className="muted">—</span> },
    { key: "lines", label: t("supplies.positions"), render: (r) => r.lines.length },
    { key: "total_cost", label: t("supplies.total"), render: (r) => som(r.total_cost) },
    {
      key: "discrepancy",
      label: t("supplies.diff"),
      // Ради этой колонки документ и заведён: сошлось с бумагой или нет.
      render: (r) =>
        r.stated_total == null ? (
          <span className="muted">—</span>
        ) : Number(r.discrepancy) === 0 ? (
          <span className="badge ok">{t("supplies.matches")}</span>
        ) : (
          <span style={{ color: "var(--danger)", fontWeight: 600 }}>
            {Number(r.discrepancy) > 0 ? "+" : ""}{som(r.discrepancy)}
          </span>
        ),
    },
    {
      key: "debt",
      label: t("supplies.debt"),
      render: (r) =>
        Number(r.debt) > 0 ? (
          <span style={{ color: "var(--danger)", fontWeight: 600 }}>{som(r.debt)}</span>
        ) : (
          <span className="badge ok">{t("supplies.paid")}</span>
        ),
    },
    {
      key: "actions",
      label: "",
      render: (r) => (
        <button className="secondary row-btn" onClick={() => setOpen(r)}>
          {t("supplies.openDoc")}
        </button>
      ),
    },
  ];

  const totalDebt = rows.reduce((s, r) => s + Number(r.debt || 0), 0);

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <p className="muted" style={{ fontSize: 13, margin: 0, maxWidth: "60ch" }}>
          {t("supplies.hint")}
        </p>
        <button onClick={startDraft}>+ {t("supplies.newDoc")}</button>
      </div>

      <div className="stat-grid" style={{ margin: "14px 0" }}>
        <div className="stat">
          <div className="label">{t("supplies.statDocs")}</div>
          <div className="value">{rows.length}</div>
        </div>
        <div className="stat">
          <div className="label">{t("supplies.statSum")}</div>
          <div className="value">{som(rows.reduce((s, r) => s + Number(r.total_cost || 0), 0))}</div>
        </div>
        <div className="stat">
          <div className="label">{t("supplies.statDebt")}</div>
          <div className="value" style={totalDebt > 0 ? { color: "var(--danger)" } : undefined}>
            {som(totalDebt)}
          </div>
        </div>
      </div>

      <DataTable columns={columns} rows={rows} />

      {/* --- Просмотр накладной --- */}
      {open && (
        <Modal
          wide
          title={`${t("supplies.docTitle")} ${open.number || `#${open.id}`}`}
          onClose={() => setOpen(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setPrinting(open)}>
                <Icon name="printer" size={16} /> {t("print.print")}
              </button>
              {isAdmin && (
                <button className="ghost row-danger" onClick={() => cancelSupply(open)}>
                  <Icon name="trash" size={16} /> {t("supplies.cancel")}
                </button>
              )}
            </>
          }
        >
          <div className="crow"><span className="k">{t("supplies.date")}</span><span>{new Date(open.received_on).toLocaleDateString("ru-RU")}</span></div>
          <div className="crow"><span className="k">{t("supplies.supplier")}</span><span>{open.supplier_name || "—"}</span></div>
          <div className="crow"><span className="k">{t("supplies.total")}</span><strong>{som(open.total_cost)}</strong></div>
          {open.stated_total != null && (
            <div className="crow">
              <span className="k">{t("supplies.statedTotal")}</span>
              <span>
                {som(open.stated_total)}{" "}
                {Number(open.discrepancy) === 0 ? (
                  <span className="badge ok">{t("supplies.matches")}</span>
                ) : (
                  <span style={{ color: "var(--danger)" }}>
                    ({t("supplies.diff")} {Number(open.discrepancy) > 0 ? "+" : ""}{som(open.discrepancy)})
                  </span>
                )}
              </span>
            </div>
          )}
          <div className="crow"><span className="k">{t("supplies.paidTo")}</span><span>{som(open.paid_amount)}</span></div>
          <div className="crow"><span className="k">{t("supplies.debt")}</span><strong style={Number(open.debt) > 0 ? { color: "var(--danger)" } : undefined}>{som(open.debt)}</strong></div>

          <table className="table plain-table" style={{ marginTop: 14 }}>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("supplies.received")}</th>
                <th>{t("supplies.lineCost")}</th>
                <th>{t("supplies.unitCost")}</th>
                <th>{t("supply.rollCode")}</th>
              </tr>
            </thead>
            <tbody>
              {open.lines.map((l) => (
                <tr key={l.id}>
                  <td><strong>{l.material_name}</strong></td>
                  <td>{q2(l.quantity)} {l.unit}</td>
                  <td>{som(l.cost)}</td>
                  <td>{q2(l.unit_cost)} <span className="muted">сом/{l.unit}</span></td>
                  <td className="muted">{l.code || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            {t("supplies.editHint")}
          </p>
        </Modal>
      )}

      {printing && <PrintSupply supply={printing} onClose={() => setPrinting(null)} />}

      {/* --- Новая накладная --- */}
      {draft && (
        <Modal
          wide
          title={t("supplies.newDoc")}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setDraft(null)}>{t("common.cancel")}</button>
              <button onClick={save} disabled={busy || !filled.length}>
                {busy ? t("common.loading") : t("supplies.post", { n: filled.length })}
              </button>
            </>
          }
        >
          <div className="row">
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("supplies.number")}</label>
              <input
                value={draft.number}
                onChange={(e) => setField("number")(e.target.value)}
                placeholder={t("supplies.numberPh")}
              />
            </div>
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("supplies.supplier")}</label>
              <RefSelect
                value={draft.supplier}
                options={suppliers}
                endpoint="/warehouse/suppliers/"
                onCreated={loadSuppliers}
                onChange={(v) => setField("supplier")(v ? Number(v) : "")}
              />
            </div>
            <div className="field" style={{ margin: 0, width: 170 }}>
              <label>{t("supplies.date")}</label>
              <input type="date" value={draft.received_on} onChange={(e) => setField("received_on")(e.target.value)} />
            </div>
          </div>

          {/* Сетка позиций: столько строк, сколько в бумажной накладной. */}
          <div className="grid-wrap" style={{ marginTop: 14 }}>
            <table className="table grid-table">
              <thead>
                <tr>
                  <th style={{ minWidth: 220 }}>{t("checkout.material")}</th>
                  <th style={{ width: 120 }}>{t("supply.form")}</th>
                  <th style={{ width: 240 }}>{t("supplies.size")}</th>
                  <th style={{ width: 110 }}>{t("supplies.received")}</th>
                  <th style={{ width: 120 }}>{t("supplies.lineCost")}</th>
                  <th style={{ width: 130 }}>{t("supply.rollCode")}</th>
                  <th style={{ width: 40 }} />
                </tr>
              </thead>
              <tbody>
                {draft.lines.map((l, i) => {
                  const m = matById[String(l.material)];
                  const qty = lineQuantity(l, m);
                  const unit = !m ? "" : !m.is_roll_material || l.form === "QTY" ? t(`unit.${m.unit}`) : "кв.м";
                  return (
                    <tr key={i}>
                      <td>
                        <select value={l.material} onChange={(e) => pickMaterial(i, e.target.value)}>
                          <option value="">—</option>
                          {materials.map((x) => (
                            <option key={x.id} value={x.id}>{x.name}</option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <select
                          value={l.form}
                          disabled={!m || !m.is_roll_material}
                          onChange={(e) => setLine(i, { form: e.target.value })}
                        >
                          <option value="SHEET">{t("supply.formSheet")}</option>
                          <option value="ROLL">{t("supply.formRoll")}</option>
                          <option value="QTY">{t("supply.formQty")}</option>
                        </select>
                      </td>
                      <td>
                        {!m || !m.is_roll_material || l.form === "QTY" ? (
                          <input
                            type="number" step="any" value={l.quantity}
                            placeholder={t("supplies.qtyPh")}
                            onChange={(e) => setLine(i, { quantity: e.target.value })}
                          />
                        ) : l.form === "ROLL" ? (
                          <div className="size-cell">
                            <input type="number" step="any" value={l.width} placeholder={t("supply.width")} onChange={(e) => setLine(i, { width: e.target.value })} />
                            <input type="number" step="any" value={l.length} placeholder={t("supply.length")} onChange={(e) => setLine(i, { length: e.target.value })} />
                          </div>
                        ) : (
                          <div className="size-cell three">
                            <input type="number" step="any" value={l.width} placeholder={t("supply.width")} onChange={(e) => setLine(i, { width: e.target.value })} />
                            <input type="number" step="any" value={l.height} placeholder={t("supply.height")} onChange={(e) => setLine(i, { height: e.target.value })} />
                            <input type="number" step="any" value={l.sheet_count} placeholder={t("supply.sheets")} onChange={(e) => setLine(i, { sheet_count: e.target.value })} />
                          </div>
                        )}
                      </td>
                      <td className="muted" style={{ whiteSpace: "nowrap" }}>
                        {qty > 0 ? `${q2(qty)} ${unit}` : "—"}
                      </td>
                      <td>
                        <input type="number" step="any" value={l.cost} onChange={(e) => setLine(i, { cost: e.target.value })} />
                      </td>
                      <td>
                        <input value={l.code} onChange={(e) => setLine(i, { code: e.target.value })} />
                      </td>
                      <td>
                        {draft.lines.length > 1 && (
                          <button className="ghost" onClick={() => dropLine(i)} aria-label={t("common.delete")}>
                            <Icon name="x" size={16} />
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <button className="ghost" style={{ marginTop: 8, color: "var(--accent-strong)", fontWeight: 600 }} onClick={addLine}>
            + {t("supplies.addLine")}
          </button>

          {/* Сверка с бумагой — ради неё документ и существует. */}
          <div className="card" style={{ background: "var(--canvas)", padding: 12, marginTop: 14 }}>
            <div className="crow">
              <span className="k">{t("supplies.total")}</span>
              <strong>{som(draftTotal)}</strong>
            </div>
            <div className="row" style={{ margin: "8px 0 0", gap: 10, flexWrap: "wrap" }}>
              <div className="field" style={{ margin: 0, width: 190 }}>
                <label>{t("supplies.statedTotal")}</label>
                <input
                  type="number" step="any" value={draft.stated_total}
                  placeholder={t("supplies.statedPh")}
                  onChange={(e) => setField("stated_total")(e.target.value)}
                />
              </div>
              <div className="field" style={{ margin: 0, width: 190 }}>
                <label>{t("supplies.paidTo")}</label>
                <input
                  type="number" step="any" value={draft.paid_amount}
                  placeholder="0"
                  onChange={(e) => setField("paid_amount")(e.target.value)}
                />
              </div>
              <div className="field grow" style={{ margin: 0 }}>
                <label>{t("supplies.note")}</label>
                <input value={draft.note} onChange={(e) => setField("note")(e.target.value)} />
              </div>
            </div>
            {stated != null && stated > 0 && (
              <p style={{ margin: "10px 0 0", fontSize: 14, color: diff === 0 ? "var(--ok)" : "var(--danger)" }}>
                {diff === 0 ? t("supplies.matchesFull") : t("supplies.diffFull", { sum: som(Math.abs(diff)) })}
              </p>
            )}
          </div>
        </Modal>
      )}
    </>
  );
}
