/**
 * Массовая переоценка «× %» (XL-05/CALC-09, волна 2).
 *
 * Поставщик поднял акрил на 7 % — раньше это 78 правок по одной карточке.
 * Здесь: отбор (тип, толщина, форма), процент и округление → предпросмотр
 * «было → стало» по каждому материалу с галочками → «Применить» к отмеченным.
 * Каждая правка пишется в журнал действий с причиной «переоценка +7 %».
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { formatNumber } from "../utils/format.js";
import { parseNumber } from "../utils/pasteTable.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const FIELDS = ["price_per_sqm", "piece_price", "price_per_unit", "price_per_pm", "wholesale_price", "cut_rate_per_pm"];
const DEFAULT_FIELDS = ["price_per_sqm", "piece_price", "price_per_unit", "price_per_pm", "wholesale_price"];

export default function RepriceModal({ types, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [percent, setPercent] = useState("");
  const [step, setStep] = useState("1");
  const [type, setType] = useState("");
  const [thickness, setThickness] = useState("");
  const [form, setForm] = useState("");
  const [fields, setFields] = useState(DEFAULT_FIELDS);
  const [rows, setRows] = useState(null);
  const [checked, setChecked] = useState({});
  const [busy, setBusy] = useState(false);

  const pct = parseNumber(percent);

  function body(extra = {}) {
    return {
      percent: pct, step, fields,
      ...(type ? { type } : {}),
      ...(thickness ? { thickness_mm: thickness } : {}),
      ...(form ? { form } : {}),
      ...extra,
    };
  }

  async function run(apply) {
    setBusy(true);
    try {
      if (apply) {
        const ids = rows.filter((r) => checked[r.id]).map((r) => r.id);
        const r = await api.post("/warehouse/materials/reprice/", body({ ids, apply: true }));
        toast(t("stock2.repriceDone", { count: r.data.applied }));
        onDone?.();
        onClose();
        return;
      }
      const r = await api.post("/warehouse/materials/reprice/", body());
      setRows(r.data.rows);
      setChecked(Object.fromEntries(r.data.rows.map((row) => [row.id, true])));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  const invalidate = (fn) => (v) => { fn(v); setRows(null); };
  const nChecked = rows ? rows.filter((r) => checked[r.id]).length : 0;

  return (
    <Modal
      wide
      title={t("stock2.repriceTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          {rows == null ? (
            <button onClick={() => run(false)} disabled={busy || pct == null || pct === 0}>
              {t("stock2.previewUpsert")}
            </button>
          ) : (
            <button onClick={() => run(true)} disabled={busy || !nChecked}>
              {t("stock2.repriceApply", { count: nChecked })}
            </button>
          )}
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("stock2.repriceHint")}</p>
      <div className="row">
        <Field className="grow" label={t("stock2.percent")}>
          <input type="text" inputMode="decimal" autoFocus value={percent} placeholder="10"
                 onChange={(e) => invalidate(setPercent)(e.target.value)} />
        </Field>
        <Field className="grow" label={t("stock2.roundTo")}>
          <select value={step} onChange={(e) => invalidate(setStep)(e.target.value)}>
            <option value="0.01">0,01</option>
            <option value="1">1</option>
            <option value="10">10</option>
          </select>
        </Field>
      </div>
      <div className="row">
        <Field className="grow" label={t("warehouse.type")}>
          <select value={type} onChange={(e) => invalidate(setType)(e.target.value)}>
            <option value="">{t("warehouse.allTypes")}</option>
            {types.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </Field>
        <Field className="grow" label={t("warehouse.thickness")}>
          <input type="text" inputMode="decimal" value={thickness}
                 onChange={(e) => invalidate(setThickness)(e.target.value)} />
        </Field>
        <Field className="grow" label={t("warehouse.stockForm")}>
          <select value={form} onChange={(e) => invalidate(setForm)(e.target.value)}>
            <option value="">{t("warehouse.allForms")}</option>
            <option value="PIECE">{t("warehouse.formPiece")}</option>
            <option value="SHEET">{t("supply.formSheet")}</option>
            <option value="ROLL">{t("supply.formRoll")}</option>
          </select>
        </Field>
      </div>
      <div className="field">
        <label>{t("stock2.whichPrices")}</label>
        <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
          {FIELDS.map((f) => (
            <label key={f} style={{ fontSize: 13, display: "flex", gap: 6, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={fields.includes(f)}
                onChange={(e) => invalidate(setFields)(e.target.checked ? [...fields, f] : fields.filter((x) => x !== f))}
              />
              {t(`stock2.field_${f}`)}
            </label>
          ))}
        </div>
      </div>

      {rows != null && (rows.length === 0 ? (
        <p className="muted">{t("stock2.repriceNothing")}</p>
      ) : (
        <div className="table-scroll">
        <table className="table plain-table">
          <thead>
            <tr>
              <th />
              <th>{t("common.name")}</th>
              <th>{t("stock2.price")}</th>
              <th>{t("stock2.beforeAfter")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.flatMap((row) =>
              row.changes.map((c, i) => (
                <tr key={`${row.id}-${c.field}`}>
                  <td>
                    {i === 0 && (
                      <input
                        type="checkbox"
                        aria-label={row.name}
                        checked={!!checked[row.id]}
                        onChange={(e) => setChecked({ ...checked, [row.id]: e.target.checked })}
                      />
                    )}
                  </td>
                  <td>{i === 0 ? row.name : ""}</td>
                  <td className="muted">{t(`stock2.field_${c.field}`)}</td>
                  <td>{formatNumber(c.before, { max: 2 })} → <strong>{formatNumber(c.after, { max: 2 })}</strong></td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        </div>
      ))}
    </Modal>
  );
}
