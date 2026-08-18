import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Какое поле ставки заводится у каждого вида — зеркало серверных `uses_*`
// (services/models.py). У готовой услуги поля берутся из ответа сервера, а
// здесь услуги ещё нет, и вид выбирают прямо в форме.
export const KIND_RATE = {
  CUTTING: ["rate_per_pm", "pricing.ratePerPm"],
  INSTALL_INTERIOR: ["rate_flat", "pricing.masterWork"],
  INSTALL_EXTERIOR: ["rate_per_piece", "pricing.ratePerPiece"],
  OTHER: ["base_price", "pricing.basePrice"],
};

// «Установка (фикс)» — legacy-вид, новые такие не заводят: для установки есть
// наружная (за букву) и внутренняя (по кв.м).
const KINDS = Object.keys(KIND_RATE);
const MACHINES = ["CNC", "LASER"];

// Создание услуги. До этого завести услугу можно было только через
// Django-админку или POST в API — на чистой базе (без seed) владелец не мог
// начать работать вовсе: резать нечем, пока нет ни одной услуги резки.
export default function ServiceFormModal({ onClose, onSaved }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [form, setForm] = useState({ name: "", kind: "CUTTING", machine: "CNC", rate: "" });
  const [busy, setBusy] = useState(false);

  const [rateField, rateLabel] = KIND_RATE[form.kind];
  const isCutting = form.kind === "CUTTING";

  async function save() {
    if (!form.name.trim()) return toast(t("pricing.needName"), "error");
    // Станок обязателен: по нему группируется отчёт резки, и услуга без него
    // всю выручку сваливает в «Без станка».
    if (isCutting && !form.machine) return toast(t("pricing.needMachine"), "error");
    setBusy(true);
    try {
      await api.post("/services/services/", {
        name: form.name.trim(),
        kind: form.kind,
        machine: isCutting ? form.machine : "",
        [rateField]: form.rate === "" ? 0 : form.rate,
      });
      toast(t("pricing.created"));
      onSaved?.();
      onClose();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("pricing.newService")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy}>{t("common.save")}</button>
        </>
      }
    >
      <div className="field">
        <label>{t("pricing.serviceName")}</label>
        <input
          autoFocus
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          placeholder={t("pricing.serviceNamePh")}
        />
      </div>

      <div className="field">
        <label>{t("pricing.serviceKindLabel")}</label>
        <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
          {KINDS.map((k) => (
            <option key={k} value={k}>{t(`serviceKind.${k}`)}</option>
          ))}
        </select>
      </div>

      {isCutting && (
        <div className="field">
          <label>{t("pricing.machine")}</label>
          <select value={form.machine} onChange={(e) => setForm({ ...form, machine: e.target.value })}>
            {MACHINES.map((m) => (
              <option key={m} value={m}>{t(`machine.${m}`)}</option>
            ))}
          </select>
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("pricing.machineHint")}</p>
        </div>
      )}

      <div className="field">
        <label>{t(rateLabel)}</label>
        <input
          type="number"
          value={form.rate}
          onChange={(e) => setForm({ ...form, rate: e.target.value })}
          placeholder="0"
        />
        {isCutting && (
          <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("pricing.ratePerPmHint")}</p>
        )}
      </div>
    </Modal>
  );
}
