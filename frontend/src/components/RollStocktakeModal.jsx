import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Промер рулона рулеткой. Не «правка остатка»: остаток тут тоже меняется, но
// вместе с ним записывается АКТ — сколько было по системе, сколько намерили и
// почему разошлось. Правкой остатка расхождение исчезает вместе с причиной, и
// через месяц на вопрос «куда делись полтора метра» ответить нечем.
const REASONS = ["SUPPLIER", "CUTTING", "DAMAGE", "MISCOUNT", "OTHER"];

export default function RollStocktakeModal({ roll, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [counted, setCounted] = useState("");
  const [reason, setReason] = useState("CUTTING");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const expected = Number(roll.metres_remaining ?? 0);
  const measured = Number(counted);
  const diff = counted === "" ? null : +(measured - expected).toFixed(2);
  // «Прочее» без объяснения — та же потерянная причина, ради которой акт и
  // заводится: строка «прочее, −1.5 м» через месяц ничего не скажет.
  const needNote = reason === "OTHER" && !note.trim();
  const valid = counted !== "" && measured >= 0 && !needNote;

  async function submit() {
    if (!valid || busy) return;
    setBusy(true);
    try {
      await api.post(`/warehouse/rolls/${roll.id}/stocktake/`, {
        counted_metres: measured,
        reason_code: reason,
        ...(note.trim() ? { note: note.trim() } : {}),
      });
      toast(t("stocktake.done"));
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
      title={t("stocktake.title", { roll: roll.code || `№${roll.id}` })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose} disabled={busy}>
            {t("common.cancel")}
          </button>
          <button onClick={submit} disabled={!valid || busy}>
            {busy ? t("common.loading") : t("stocktake.submit")}
          </button>
        </>
      }
    >
      <div className="crow">
        <span className="k">{t("stocktake.expected")}</span>
        <strong>{expected} {t("unit.METER")}</strong>
      </div>
      <div className="field">
        <label>{t("stocktake.counted")}</label>
        <input
          type="number"
          step="any"
          value={counted}
          onChange={(e) => setCounted(e.target.value)}
          autoFocus
        />
      </div>
      {/* Расхождение показываем сразу, пока рулетка ещё в руках: цифру проще
          перепроверить здесь, чем спорить о ней через неделю. */}
      {diff !== null && diff !== 0 && (
        <p style={{ color: diff < 0 ? "var(--danger)" : "var(--accent-strong)", margin: "-4px 0 10px" }}>
          {diff < 0
            ? t("stocktake.short", { n: Math.abs(diff) })
            : t("stocktake.surplus", { n: diff })}
        </p>
      )}
      <div className="field">
        <label>{t("stocktake.reason")}</label>
        <select value={reason} onChange={(e) => setReason(e.target.value)}>
          {REASONS.map((code) => (
            <option key={code} value={code}>{t(`stocktake.reason${code}`)}</option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>{t("stocktake.note")}{reason === "OTHER" ? " *" : ""}</label>
        <input value={note} onChange={(e) => setNote(e.target.value)} />
        {needNote && (
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
            {t("stocktake.noteRequired")}
          </p>
        )}
      </div>
    </Modal>
  );
}
