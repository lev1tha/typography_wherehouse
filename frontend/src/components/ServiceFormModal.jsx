import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Какие поля ставки заводятся у каждого вида — зеркало серверных `uses_*`
// (services/models.py). У готовой услуги поля берутся из ответа сервера, а
// здесь услуги ещё нет, и вид выбирают прямо в форме.
//
// Список, а не одно поле: у отходов мерок три (квадраты, метры, штуки), и
// цена на каждую своя.
export const KIND_RATE = {
  CUTTING: [["rate_per_pm", "pricing.ratePerPm"]],
  // Гравировка — цена за кв.м гравируемой площади; в кассе её правят по заказу.
  ENGRAVING: [["rate_flat", "pricing.engravingRate"]],
  // Отходы — продажа обрезков и брака: мерку и цену называют в кассе, а здесь
  // задаётся базовый прайс по каждой мерке.
  WASTE: [
    ["rate_flat", "pricing.wasteRateSqm"],
    ["rate_per_pm", "pricing.wasteRatePm"],
    ["rate_per_piece", "pricing.wasteRatePiece"],
  ],
  INSTALL_INTERIOR: [["rate_flat", "pricing.masterWork"]],
  INSTALL_EXTERIOR: [["rate_per_piece", "pricing.ratePerPiece"]],
  OTHER: [["base_price", "pricing.basePrice"]],
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
  // Ставки держим по ИМЕНИ ПОЛЯ: у отходов их три, и общий `rate` свалил бы
  // цену за квадрат и цену за штуку в одно число.
  const [form, setForm] = useState({ name: "", kind: "CUTTING", machine: "CNC", rates: {} });
  const [busy, setBusy] = useState(false);

  const rateFields = KIND_RATE[form.kind];
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
        ...Object.fromEntries(
          rateFields.map(([key]) => [key, form.rates[key] === undefined || form.rates[key] === "" ? 0 : form.rates[key]])
        ),
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
        <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value, rates: {} })}>
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

      {rateFields.map(([key, label]) => (
        <div className="field" key={key}>
          <label>{t(label)}</label>
          <input
            type="number"
            value={form.rates[key] ?? ""}
            onChange={(e) => setForm({ ...form, rates: { ...form.rates, [key]: e.target.value } })}
            placeholder="0"
          />
          {isCutting && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("pricing.ratePerPmHint")}</p>
          )}
          {form.kind === "ENGRAVING" && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("pricing.engravingRateHint")}</p>
          )}
        </div>
      ))}
      {form.kind === "WASTE" && (
        <p className="muted" style={{ fontSize: 12, margin: "-8px 0 0" }}>{t("pricing.wasteRateHint")}</p>
      )}
    </Modal>
  );
}
