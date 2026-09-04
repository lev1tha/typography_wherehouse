import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Отход (брак) — сеткой, теми же мерками, что и приход (2026-09-04, просьба
// владельца): лист — размер × количество листов, рулон — метры с рулона,
// штучное — количество, или сразу площадью. Каждая строка уходит обычным
// списанием, себестоимость выброшенного пишется в журнал склада.

const q2 = (n) => Number(n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
const today = () => new Date().toLocaleDateString("sv-SE");

const EMPTY_LINE = {
  material: "", form: "SHEET",
  width: "", height: "", sheet_count: "", area: "", length: "", quantity: "", roll: "", note: "",
};

// Какими мерками считается отход у материала — те же, что у прихода.
function formsFor(m) {
  if (!m) return ["SHEET"];
  if (!m.is_roll_material) return ["QTY"];
  if (m.sells_by_metre) return ["ROLL", "AREA"];
  return ["SHEET", "AREA"];
}

/** Сколько уйдёт со склада — та же арифметика, что на сервере (`waste.line_quantity`). */
function lineQuantity(line, m, roll) {
  if (!m) return 0;
  const n = (v) => Number(v) || 0;
  if (!m.is_roll_material) return n(line.quantity);
  if (line.form === "AREA") return n(line.area);
  if (line.form === "ROLL") return (Number(roll?.width) || Number(m.roll_width) || 0) * n(line.length);
  return n(line.width) * n(line.height) * n(line.sheet_count);
}

export default function WasteModal({ materials, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [rolls, setRolls] = useState([]);
  const [date, setDate] = useState(today());
  const [note, setNote] = useState("");
  const [lines, setLines] = useState([{ ...EMPTY_LINE }]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get("/warehouse/rolls/", { params: { page_size: 500 } })
      .then((r) => setRolls((r.data.results ?? r.data).filter((x) => Number(x.remaining_area) > 0)))
      .catch(() => setRolls([]));
  }, []);

  const matById = useMemo(() => Object.fromEntries(materials.map((m) => [String(m.id), m])), [materials]);
  const setLine = (i, patch) => setLines((ls) => ls.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));
  const addLine = () => setLines((ls) => [...ls, { ...EMPTY_LINE }]);
  const dropLine = (i) => setLines((ls) => ls.filter((_, idx) => idx !== i));

  // Партии материала: у листа — пачки, у рулона — рулоны, у штучного —
  // поставки. Початый первым, как и режут; пусто — FIFO решает сервер.
  const lotsFor = (m) => {
    if (!m) return [];
    const form = !m.is_roll_material ? "PIECE" : m.sells_by_metre ? "ROLL" : "SHEET";
    return rolls
      .filter((r) => r.material === m.id && r.form === form)
      .sort((a, b) => new Date(a.received_at) - new Date(b.received_at));
  };
  const lotLabel = (r, m) => {
    const name = r.code || `№${r.id}`;
    const left = m.sells_by_metre && r.metres_remaining != null
      ? `${q2(r.metres_remaining)} ${t("unit.METER")}`
      : `${q2(r.remaining_area)} ${m.is_roll_material ? t("unit.SQM") : t(`unit.${m.unit || "PIECE"}`)}`;
    return `${name} · ${left}`;
  };
  const rollOf = (l) => rolls.find((r) => String(r.id) === String(l.roll)) || null;

  function pickMaterial(i, id) {
    const m = matById[String(id)];
    const form = formsFor(m)[0];
    setLine(i, {
      material: id, form, roll: "",
      // Размер листа — из карточки, как в накладной: он у материала постоянный.
      width: form === "SHEET" && m?.sheet_width ? String(m.sheet_width) : "",
      height: form === "SHEET" && m?.sheet_height ? String(m.sheet_height) : "",
      sheet_count: "", area: "", length: "", quantity: "",
    });
  }

  const started = (l) =>
    l.material || l.width !== "" || l.height !== "" || l.sheet_count !== "" ||
    l.area !== "" || l.length !== "" || l.quantity !== "" || l.note !== "";
  const qtyOf = (l) => lineQuantity(l, matById[String(l.material)], rollOf(l));
  const unitOf = (m) => (!m ? "" : m.is_roll_material ? t("unit.SQM") : t(`unit.${m.unit}`));
  const problem = (l) => {
    if (!started(l)) return null;
    const m = matById[String(l.material)];
    if (!m) return t("supplies.lineNeedsMaterial");
    const qty = qtyOf(l);
    if (!(qty > 0)) return t("waste.lineNeedsQty");
    // Нехватка видна сразу, в строке, а не после нажатия.
    if (qty > Number(m.quantity)) return t("waste.notEnough", { left: `${q2(m.quantity)} ${unitOf(m)}` });
    return null;
  };
  const problems = lines.map((l, i) => ({ i, text: problem(l) })).filter((x) => x.text);
  const filled = lines.filter((l) => started(l) && !problem(l));

  async function save() {
    if (problems.length) return toast(t("waste.linesIncomplete", { rows: problems.map((x) => x.i + 1).join(", ") }), "error");
    if (!filled.length) return toast(t("waste.needLines"), "error");
    setBusy(true);
    try {
      const payload = {
        happened_on: date || null,
        note,
        lines: filled.map((l) => {
          const m = matById[String(l.material)];
          const base = { material: Number(l.material), note: l.note, roll: l.roll ? Number(l.roll) : null };
          if (!m.is_roll_material) return { ...base, form: "QTY", quantity: Number(l.quantity) };
          if (l.form === "AREA") return { ...base, form: "AREA", area: Number(l.area) };
          if (l.form === "ROLL") return { ...base, form: "ROLL", length: Number(l.length) };
          return {
            ...base, form: "SHEET",
            width: Number(l.width), height: Number(l.height), sheet_count: Number(l.sheet_count),
          };
        }),
      };
      await api.post("/warehouse/waste/", payload);
      toast(t("waste.done"));
      onDone?.();
      onClose();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      wide
      title={t("waste.new")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy || !filled.length || problems.length > 0}>
            {busy ? t("common.loading") : t("waste.submit", { n: filled.length })}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("waste.hint")}</p>
      <div className="row">
        <div className="field" style={{ margin: 0, width: 170 }}>
          <label>{t("waste.date")}</label>
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </div>
        <div className="field grow" style={{ margin: 0 }}>
          <label>{t("waste.note")}</label>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("waste.notePh")} />
        </div>
      </div>

      <div className="grid-wrap" style={{ marginTop: 14 }}>
        <table className="table grid-table">
          <thead>
            <tr>
              <th style={{ minWidth: 200 }}>{t("checkout.material")}</th>
              <th style={{ width: 150 }}>{t("waste.form")}</th>
              <th style={{ width: 240 }}>{t("supplies.size")}</th>
              <th style={{ width: 110 }}>{t("waste.amount")}</th>
              <th style={{ width: 170 }}>{t("waste.lot")}</th>
              <th style={{ width: 160 }}>{t("waste.lineNote")}</th>
              <th style={{ width: 40 }} />
            </tr>
          </thead>
          <tbody>
            {lines.map((l, i) => {
              const m = matById[String(l.material)];
              const forms = formsFor(m);
              const lots = lotsFor(m);
              const qty = qtyOf(l);
              const err = problem(l);
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
                    <select value={l.form} disabled={forms.length < 2} onChange={(e) => setLine(i, { form: e.target.value })}>
                      {forms.map((f) => (
                        <option key={f} value={f}>{t(`waste.form${f[0]}${f.slice(1).toLowerCase()}`)}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    {!m || !m.is_roll_material ? (
                      <input
                        type="number" step="any" value={l.quantity}
                        placeholder={t("supplies.qtyPh")}
                        onChange={(e) => setLine(i, { quantity: e.target.value })}
                      />
                    ) : l.form === "AREA" ? (
                      <input
                        type="number" step="any" value={l.area}
                        placeholder={t("waste.area")}
                        onChange={(e) => setLine(i, { area: e.target.value })}
                      />
                    ) : l.form === "ROLL" ? (
                      <input
                        type="number" step="any" value={l.length}
                        placeholder={t("waste.metres")}
                        onChange={(e) => setLine(i, { length: e.target.value })}
                      />
                    ) : (
                      <div className="size-cell three">
                        <input type="number" step="any" value={l.width} placeholder={t("supply.width")} onChange={(e) => setLine(i, { width: e.target.value })} />
                        <input type="number" step="any" value={l.height} placeholder={t("supply.height")} onChange={(e) => setLine(i, { height: e.target.value })} />
                        <input type="number" step="any" value={l.sheet_count} placeholder={t("supply.sheets")} onChange={(e) => setLine(i, { sheet_count: e.target.value })} />
                      </div>
                    )}
                    {err && l.material && (
                      <div style={{ color: "var(--danger)", fontSize: 11, marginTop: 2 }}>{err}</div>
                    )}
                  </td>
                  <td className="muted" style={{ whiteSpace: "nowrap" }}>
                    {qty > 0 ? `${q2(qty)} ${unitOf(m)}` : "—"}
                  </td>
                  <td>
                    {lots.length > 0 ? (
                      <select value={l.roll} onChange={(e) => setLine(i, { roll: e.target.value })}>
                        <option value="">{t("waste.lotFifo")}</option>
                        {lots.map((r) => (
                          <option key={r.id} value={r.id}>{lotLabel(r, m)}</option>
                        ))}
                      </select>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    <input value={l.note} onChange={(e) => setLine(i, { note: e.target.value })} />
                  </td>
                  <td>
                    {lines.length > 1 && (
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
      {problems.length > 0 && (
        <p style={{ color: "var(--danger)", fontSize: 13, margin: "8px 0 0" }}>
          {t("waste.linesIncomplete", { rows: problems.map((x) => x.i + 1).join(", ") })}
        </p>
      )}
    </Modal>
  );
}
