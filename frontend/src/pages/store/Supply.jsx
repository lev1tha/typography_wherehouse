import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import StockJournal from "../../components/StockJournal.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import Tabs from "../../components/Tabs.jsx";
import { parseNumber } from "../../utils/pasteTable.js";

// Module-level so they keep a stable identity across renders — defining these
// inside the component remounts the inputs on every keystroke (focus loss).
const Field = ({ label, children }) => (
  <div className="field">
    <label>{label}</label>
    {children}
  </div>
);
// Текстовое поле с десятичной клавиатурой: «17,8608» с запятой, как в Excel
// (type=number на запятой отдавал пустую строку).
const Num = ({ value, onChange }) => (
  <input
    type="text"
    inputMode="decimal"
    value={value}
    aria-invalid={value !== "" && parseNumber(value) == null ? "true" : undefined}
    onChange={(e) => onChange(e.target.value)}
  />
);
const today = () => new Date().toLocaleDateString("sv-SE");
// Листовой материал считают листами (XL-03/STK-08): сервер переводит их в кв.м
// по площади листа без округления до сотых.
const bySheet = (m) => !!(m && m.is_roll_material && !m.sells_by_metre && Number(m.piece_area) > 0);
const num = (v) => parseNumber(v);
const q4 = (n) => (Math.round(n * 10000) / 10000).toString();
// Переключатель «листы / кв.м» — внизу того же поля.
const UnitToggle = ({ value, onChange, t }) => (
  <div className="tabs" style={{ marginBottom: 10 }}>
    {[[true, t("warehouse.unitSheet")], [false, t("unit.SQM")]].map(([key, label]) => (
      <button key={String(key)} type="button" className={value === key ? "active" : ""} onClick={() => onChange(key)}>
        {label}
      </button>
    ))}
  </div>
);
const MaterialSelect = ({ value, onChange, materials, t }) => (
  <select value={value} onChange={onChange}>
    <option value="">—</option>
    {materials.map((m) => (
      <option key={m.id} value={m.id}>
        {/* Рулон меряют метрами — их и показываем, а не квадраты. */}
        {m.name} ({t("supply.currentStock")}:{" "}
        {m.sells_by_metre && m.metres_remaining != null
          ? `${m.metres_remaining} ${t("unit.METER")}`
          : `${m.quantity} ${t(`unit.${m.unit}`)}`})
      </option>
    ))}
  </select>
);

const WRITEOFF_REASONS = ["DAMAGE", "DEFECT", "LOSS", "EXPIRY", "OTHER"];

// «Движение»: журнал всех операций склада + две формы, инвентаризация
// (пересчёт) и списание (порча/брак/утеря). Приём нового прихода вынесен на
// строку материала в «Материалах» (ReceiveStockModal).
export default function Supply({ embedded = false }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [tab, setTab] = useState("log");
  const [materials, setMaterials] = useState([]);
  const [busy, setBusy] = useState(false);

  const INV0 = { material: "", counted_quantity: "", reason: "", inSheets: true, day: today() };
  const WO0 = { material: "", roll: "", quantity: "", reason_code: "DAMAGE", note: "", inSheets: true, day: today() };
  const [inv, setInv] = useState(INV0);
  const [writeoff, setWriteoff] = useState(WO0);
  // Рулоны выбранного рулонного материала: брак списывают с конкретного рулона
  // и в метрах — общее число в кв.м уходило FIFO со старейшего рулона, и
  // «порвали 2 м рулона №8» обнуляло целый №7.
  const [rolls, setRolls] = useState([]);

  function load() {
    // page_size: каталог нужен целиком — это выпадающий список, а не таблица.
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 200 } })
      .then((r) => setMaterials(r.data.results));
  }
  useEffect(load, []);

  const stockOf = (id) => {
    const m = materials.find((x) => x.id === Number(id));
    return m ? Number(m.quantity) : null;
  };
  const materialOf = (id) => materials.find((x) => x.id === Number(id)) || null;
  const writeoffMat = materialOf(writeoff.material);
  const writeoffByRoll = !!writeoffMat?.sells_by_metre;
  const woSheets = bySheet(writeoffMat) && writeoff.inSheets;
  const writeoffUnit = writeoffMat
    ? writeoffByRoll ? t("unit.METER") : woSheets ? t("warehouse.unitSheet") : writeoffMat.is_roll_material ? t("unit.SQM") : t(`unit.${writeoffMat.unit}`)
    : "";
  // Сколько уйдёт в единицах хранения (кв.м у листа) — для строки «станет».
  const woQty = num(writeoff.quantity);
  const woArea = woQty == null ? null : woSheets ? woQty * Number(writeoffMat.piece_area) : woQty;
  const invMat = materialOf(inv.material);
  const invSheets = bySheet(invMat) && inv.inSheets;
  const invQty = num(inv.counted_quantity);
  const invArea = invQty == null ? null : invSheets ? invQty * Number(invMat.piece_area) : invQty;
  useEffect(() => {
    if (!writeoffByRoll) {
      setRolls([]);
      return;
    }
    api
      .get("/warehouse/rolls/", { params: { material: writeoff.material, page_size: 500 } })
      .then((r) =>
        setRolls(
          (r.data.results ?? r.data)
            .filter((x) => x.form === "ROLL" && Number(x.metres_remaining) > 0)
            .sort((a, b) => new Date(a.received_at) - new Date(b.received_at))
        )
      )
      .catch(() => setRolls([]));
  }, [writeoff.material, writeoffByRoll]);
  const writeoffRoll = rolls.find((r) => String(r.id) === String(writeoff.roll)) || rolls[0] || null;
  const rollLabel = (r) => `${r.code || `№${r.id}`} · ${r.metres_remaining} ${t("unit.METER")}`;

  async function run(fn) {
    setBusy(true);
    try {
      await fn();
      toast(t("supply.done"));
      load();
    } catch (e) {
      const data = e.response?.data;
      const first = data && (data.detail || (typeof data === "object" ? Object.values(data)[0] : data));
      toast(Array.isArray(first) ? first[0] : first || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  const submitInventory = () =>
    run(async () => {
      await api.post("/warehouse/materials/adjust/", {
        material: Number(inv.material),
        ...(invSheets ? { counted_sheets: invQty } : { counted_quantity: invQty }),
        reason: inv.reason,
        happened_on: inv.day || null,
      });
      setInv(INV0);
    });

  const submitWriteOff = () =>
    run(async () => {
      if (writeoffByRoll) {
        // С конкретного рулона и в метрах — его шириной и по его цене.
        await api.post(`/warehouse/rolls/${writeoffRoll.id}/write-off/`, {
          metres: woQty,
          reason_code: writeoff.reason_code,
          note: writeoff.note,
          happened_on: writeoff.day || null,
        });
      } else {
        await api.post("/warehouse/materials/write-off/", {
          material: Number(writeoff.material),
          ...(woSheets ? { sheets: woQty } : { quantity: woQty }),
          reason_code: writeoff.reason_code,
          note: writeoff.note,
          happened_on: writeoff.day || null,
        });
      }
      setWriteoff(WO0);
    });

  const TABS = [
    ["log", t("journal.title")],
    ["inventory", t("supply.inventory")],
    ["writeoff", t("supply.writeoff")],
  ];

  return (
    <>
      {!embedded && <h1>{t("supply.title")}</h1>}
      <Tabs
        id="supply"
        panel={false}
        label={t("supply.title")}
        value={tab}
        onChange={setTab}
        tabs={TABS.map(([key, label]) => ({ key, label }))}
      />

      {tab === "log" && <StockJournal />}

      {tab === "inventory" && (
        <div className="card" style={{ maxWidth: 520 }}>
          <Field label={t("checkout.material")}>
            {/* Рулонных материалов здесь нет: их сверяют промером КАЖДОГО рулона
                и в метрах (Склад → «Промер»), а общее число в кв.м списывало бы
                расхождение FIFO со старейшего рулона. Сервер такую правку и не
                примет — не предлагаем то, что не сработает. */}
            <MaterialSelect
              value={inv.material}
              onChange={(e) => setInv({ ...inv, material: e.target.value })}
              materials={materials.filter((m) => !m.sells_by_metre)}
              t={t}
            />
          </Field>
          {materials.some((m) => m.sells_by_metre) && (
            <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>{t("warehouse.rollStockByMeasure")}</p>
          )}
          {bySheet(invMat) && (
            <UnitToggle value={inv.inSheets} onChange={(v) => setInv({ ...inv, inSheets: v, counted_quantity: "" })} t={t} />
          )}
          <Field label={`${t("supply.counted")}${invSheets ? `, ${t("warehouse.unitSheet")}` : ""}`}>
            <Num value={inv.counted_quantity} onChange={(v) => setInv({ ...inv, counted_quantity: v })} />
          </Field>
          <Field label={t("stock2.countedOn")}>
            <input type="date" value={inv.day} max={today()} onChange={(e) => setInv({ ...inv, day: e.target.value })} />
          </Field>
          {inv.material && invArea != null && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12, marginBottom: 14 }}>
              <div className="crow"><span className="k">{t("supply.becomes")}</span><strong>{stockOf(inv.material)} → {q4(invArea)}</strong></div>
            </div>
          )}
          <Field label={t("supply.reason")}>
            <input value={inv.reason} onChange={(e) => setInv({ ...inv, reason: e.target.value })} />
          </Field>
          <button style={{ width: "100%", height: 50 }} onClick={submitInventory} disabled={busy || !inv.material || invQty == null || invQty < 0}>
            {t("supply.inventory")}
          </button>
        </div>
      )}

      {tab === "writeoff" && (
        <div className="card" style={{ maxWidth: 520 }}>
          <Field label={t("checkout.material")}>
            <MaterialSelect
              value={writeoff.material}
              onChange={(e) => setWriteoff({ ...writeoff, material: e.target.value, roll: "", quantity: "", inSheets: true })}
              materials={materials}
              t={t}
            />
          </Field>
          {/* Рулон: списывают с конкретного рулона и в метрах. Общее число в
              кв.м сервер для него не примет — оно шло FIFO со старейшего. */}
          {writeoffByRoll && (
            <>
              <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>{t("supply.writeoffRollHint")}</p>
              <Field label={t("supply.writeoffRoll")}>
                {rolls.length ? (
                  <select value={writeoffRoll ? writeoffRoll.id : ""} onChange={(e) => setWriteoff({ ...writeoff, roll: e.target.value })}>
                    {rolls.map((r) => (
                      <option key={r.id} value={r.id}>{rollLabel(r)}</option>
                    ))}
                  </select>
                ) : (
                  <p className="muted" style={{ fontSize: 13, margin: 0 }}>{t("supply.writeoffRollNone")}</p>
                )}
              </Field>
            </>
          )}
          {bySheet(writeoffMat) && (
            <UnitToggle value={writeoff.inSheets} onChange={(v) => setWriteoff({ ...writeoff, inSheets: v, quantity: "" })} t={t} />
          )}
          <Field label={writeoffByRoll ? t("supply.writeoffMetres") : `${t("supply.writeoffQty")}${writeoffUnit ? `, ${writeoffUnit}` : ""}`}>
            <Num value={writeoff.quantity} onChange={(v) => setWriteoff({ ...writeoff, quantity: v })} />
          </Field>
          <Field label={t("stock2.writtenOn")}>
            <input type="date" value={writeoff.day} max={today()} onChange={(e) => setWriteoff({ ...writeoff, day: e.target.value })} />
          </Field>
          {writeoff.material && woArea != null && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12, marginBottom: 14 }}>
              {writeoffByRoll ? (
                writeoffRoll && (
                  <div className="crow">
                    <span className="k">{t("supply.becomes")} ({rollLabel(writeoffRoll).split(" · ")[0]})</span>
                    <strong>
                      {writeoffRoll.metres_remaining} → {(Number(writeoffRoll.metres_remaining) - woQty).toFixed(2)} {t("unit.METER")}
                    </strong>
                  </div>
                )
              ) : (
                <div className="crow"><span className="k">{t("supply.becomes")}</span><strong>{stockOf(writeoff.material)} → {q4(Math.max(0, Number(stockOf(writeoff.material)) - woArea))} {writeoffMat?.is_roll_material ? t("unit.SQM") : writeoffUnit}</strong></div>
              )}
            </div>
          )}
          <Field label={t("supply.writeoffReason")}>
            <select value={writeoff.reason_code} onChange={(e) => setWriteoff({ ...writeoff, reason_code: e.target.value })}>
              {WRITEOFF_REASONS.map((code) => (
                <option key={code} value={code}>{t(`writeoffReason.${code}`)}</option>
              ))}
            </select>
          </Field>
          <Field label={t("supply.note")}>
            <input value={writeoff.note} onChange={(e) => setWriteoff({ ...writeoff, note: e.target.value })} />
          </Field>
          <button
            className="danger"
            style={{ width: "100%", height: 50 }}
            onClick={submitWriteOff}
            disabled={
              busy || !writeoff.material || !(woQty > 0) ||
              (writeoffByRoll && (!writeoffRoll || woQty > Number(writeoffRoll.metres_remaining)))
            }
          >
            {t("supply.writeoff")}
          </button>
        </div>
      )}
    </>
  );
}
