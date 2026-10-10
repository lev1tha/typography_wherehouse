import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatNumber } from "../utils/format.js";
import { parseNumber } from "../utils/pasteTable.js";
import Field from "./Field.jsx";

const today = () => new Date().toLocaleDateString("sv-SE");

// Исправление остатка — инвентаризация одного материала.
//
// Заказчик: «внесли 500 вместо 50, и поменять уже никак». Так и было: приход
// добавляет, продажа списывает, а поставить остаток равным правде было нечем —
// эндпоинт `/materials/adjust/` существовал, но в интерфейс выведен не был.
//
// Правим НЕ полем в карточке материала: остаток — не свойство товара, а итог
// движений. Поэтому расхождение проходит той же дорогой, что продажа и приход:
// пишется в журнал операцией «Инвентаризация», а у рулонного материала ещё и
// разбирается по партиям (иначе число в материале и площади партий разъедутся,
// и себестоимость проданного начнёт врать).
export default function AdjustStockModal({ material, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const roll = !!material.is_roll_material;
  // Площадь листа этого материала — по ней остаток переводится в листы. Нет
  // размера листа — считать нечего, остаётся ввод в кв.м.
  const sheetArea = Number(material.piece_area) || 0;
  const unit = t(`unit.${material.unit}`);
  const wholeUnit =
    (material.intake_form || "SHEET") === "ROLL"
      ? t("warehouse.unitRoll")
      : t("warehouse.unitSheet");

  // Листовой материал считают ЛИСТАМИ: «осталось 4 листа», а не «13,33 кв.м».
  // Поэтому у рулонного с известной площадью листа ввод по умолчанию в листах,
  // а в кв.м можно переключиться.
  const [inSheets, setInSheets] = useState(roll && sheetArea > 0);
  const [counted, setCounted] = useState("");
  const [reason, setReason] = useState("");
  // Дата пересчёта (G3-N3): считали 30.09, вносят 2.10 — недостача должна
  // лечь в сентябрь, а не в октябрь.
  const [day, setDay] = useState(today());
  const [busy, setBusy] = useState(false);

  const cur = Number(material.quantity) || 0;
  const enteredUnit = roll ? (inSheets ? wholeUnit : t("unit.SQM")) : unit;
  // Введённое число → количество в единицах хранения (кв.м у рулонного).
  // Запятая — как в русском Excel: «17,8608».
  const countedNum = counted === "" ? null : parseNumber(counted);
  const target =
    countedNum == null ? null : inSheets ? countedNum * sheetArea : countedNum;
  const delta = target == null ? 0 : target - cur;
  // Кв.м показываем до 4 знаков: лист 1.22×2.44 = 2.9768, и «6 листов» с
  // двумя знаками превращались в 17.86 — последний лист было не продать (XL-03).

  async function submit() {
    if (target == null || target < 0) return;
    setBusy(true);
    try {
      await api.post("/warehouse/materials/adjust/", {
        material: material.id,
        // Листами — сервер сам переведёт по площади листа без округления.
        ...(inSheets
          ? { counted_sheets: countedNum }
          : { counted_quantity: Number(target.toFixed(4)) }),
        reason,
        happened_on: day || null,
      });
      toast(t("supply.done"));
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
      title={`${t("supply.inventory")}: ${material.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || target == null || target < 0}>
            {t("common.save")}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        {t("supply.adjustHint")}
      </p>

      <div className="crow">
        <span className="k">{t("supply.currentStock")}</span>
        <strong>
          {formatNumber(cur, { max: 4 })} {roll ? t("unit.SQM") : unit}
          {roll && sheetArea > 0 && (
            <span className="muted" style={{ fontWeight: 400 }}>
              {" "}· ≈{formatNumber(cur / sheetArea, { max: 2 })} {t("warehouse.sheetsShort")}
            </span>
          )}
        </strong>
      </div>

      {roll && sheetArea > 0 && (
        <div className="tabs" style={{ marginTop: 12 }}>
          {[[true, wholeUnit], [false, t("unit.SQM")]].map(([key, label]) => (
            <button
              key={String(key)}
              className={inSheets === key ? "active" : ""}
              onClick={() => { setInSheets(key); setCounted(""); }}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      <Field style={{ marginTop: 12 }} label={<>{`${t("supply.counted")}, ${enteredUnit}`}</>}>
        <input
          type="text"
          inputMode="decimal"
          autoFocus
          value={counted}
          aria-invalid={counted !== "" && countedNum == null ? "true" : undefined}
          onChange={(e) => setCounted(e.target.value)}
        />
      </Field>

      <Field label={t("stock2.countedOn")}>
        <input type="date" value={day} max={today()} onChange={(e) => setDay(e.target.value)} />
      </Field>

      <Field label={t("supply.reason")}>
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={t("supply.adjustReasonPh")}
        />
      </Field>

      {target != null && target >= 0 && (
        <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
          <div className="crow">
            <span className="k">{t("supply.becomes")}</span>
            <strong>
              {formatNumber(cur, { max: 4 })} → {formatNumber(target, { max: 4 })} {roll ? t("unit.SQM") : unit}
            </strong>
          </div>
          <div className="crow">
            <span className="k">{t("supply.diff")}</span>
            <strong style={{ color: delta < 0 ? "var(--danger-ink)" : "var(--ok-ink)" }}>
              {delta > 0 ? "+" : ""}{formatNumber(delta, { max: 4 })} {roll ? t("unit.SQM") : unit}
            </strong>
          </div>
        </div>
      )}
    </Modal>
  );
}
