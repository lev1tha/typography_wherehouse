import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Icon from "./Icon.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatMoney, formatNumber } from "../utils/format.js";
import Field from "./Field.jsx";

const som = (n) => formatMoney(n);
const monthLabel = (ym) => {
  if (!ym) return "";
  const [y, m] = ym.split("-");
  return `${m}.${y}`;
};

// Настройки, от которых зависят амортизация и налог (2026-10-07):
//   · порог капвложения — покупка в «Инвестициях» от этой суммы становится
//     активом с амортизацией, дешевле — сразу расходом (D-22, 20 000 сом);
//   · «аренда до» — улучшение арендованного цеха амортизируется не дольше
//     аренды (D-13);
//   · ставка налога с выручки — историей с месяцем начала: новая ставка не
//     пересчитывает прошлые месяцы (D-10, 4 % с октября 2026).
// У уже внесённых покупок решение и срок записаны в них самих — правка порога
// или аренды прошлое не трогает.
export default function AssetTaxSettings({ settings, readOnly, onSettings, onChanged }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rates, setRates] = useState([]);
  const [draft, setDraft] = useState({ valid_from: "", rate: "", basis: "ACCRUAL" });

  function loadRates() {
    api.get("/finance/tax-rates/")
      .then((r) => setRates(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(loadRates, []);

  function save(field, value) {
    api.patch("/finance/settings/", { [field]: value })
      .then((r) => {
        onSettings?.(r.data);
        onChanged?.();
      })
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }

  function addRate() {
    if (!draft.valid_from || draft.rate === "") return toast(t("taxRates.needBoth"), "error");
    api.post("/finance/tax-rates/", { valid_from: draft.valid_from, rate: draft.rate, basis: draft.basis })
      .then(() => {
        setDraft({ valid_from: "", rate: "", basis: draft.basis });
        loadRates();
        onChanged?.();
        toast(t("taxRates.added"));
      })
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }

  // Основа налога — часть истории ставки: меняется с её месяца и подчиняется
  // замку периода (прошлое не переписывается).
  function changeBasis(rate, basis) {
    api.patch(`/finance/tax-rates/${rate.id}/`, { basis })
      .then(() => {
        loadRates();
        onChanged?.();
        toast(t("common.saved"));
      })
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }

  async function removeRate(rate) {
    if (!(await confirm(t("taxRates.confirmDelete", { month: monthLabel(rate.valid_from) })))) return;
    api.delete(`/finance/tax-rates/${rate.id}/`)
      .then(() => {
        loadRates();
        onChanged?.();
      })
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>{t("assetTax.title")}</h3>
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("assetTax.hint")}</p>

      <div className="crow">
        <span className="k">
          {t("assetTax.threshold")}
          <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{t("assetTax.thresholdHint")}</div>
        </span>
        {readOnly ? (
          <strong>{som(settings.capitalization_threshold)}</strong>
        ) : (
          <input
            type="number"
            min="0"
            value={settings.capitalization_threshold ?? ""}
            onChange={(e) => onSettings?.({ ...settings, capitalization_threshold: e.target.value })}
            onBlur={(e) => save("capitalization_threshold", e.target.value === "" ? 0 : e.target.value)}
            style={{ width: 150, height: 34, textAlign: "right" }}
          />
        )}
      </div>

      <div className="crow">
        <span className="k">
          {t("assetTax.leaseUntil")}
          <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{t("assetTax.leaseUntilHint")}</div>
        </span>
        {readOnly ? (
          <strong>{settings.lease_until || t("assetTax.notSet")}</strong>
        ) : (
          <input
            type="date"
            value={settings.lease_until || ""}
            onChange={(e) => save("lease_until", e.target.value || null)}
            style={{ width: 170, height: 34 }}
          />
        )}
      </div>

      <h4 style={{ margin: "18px 0 2px" }}>{t("taxRates.title")}</h4>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("taxRates.hint")}</p>
      {rates.length === 0 ? (
        <p className="muted">{t("taxRates.empty")}</p>
      ) : (
        rates.map((rate) => (
          <div key={rate.id} className="crow" style={{ borderBottom: "1px solid var(--hairline)" }}>
            <span>
              {t("taxRates.since", { month: monthLabel(rate.valid_from) })}
              {rate.note && <div className="muted" style={{ fontSize: 12 }}>{rate.note}</div>}
            </span>
            <span className="row" style={{ gap: 4, margin: 0, alignItems: "center" }}>
              <strong>{formatNumber(rate.rate)} %</strong>
              {readOnly ? (
                <span className="chip">{t(`taxRates.basis_${rate.basis || "ACCRUAL"}`)}</span>
              ) : (
                <select
                  aria-label={t("taxRates.basis")}
                  value={rate.basis || "ACCRUAL"}
                  onChange={(e) => changeBasis(rate, e.target.value)}
                  style={{ minWidth: 0, width: 260, height: 34 }}
                >
                  <option value="ACCRUAL">{t("taxRates.basis_ACCRUAL")}</option>
                  <option value="CASH">{t("taxRates.basis_CASH")}</option>
                </select>
              )}
              {!readOnly && (
                <button className="ghost" onClick={() => removeRate(rate)} aria-label={t("common.delete")}>
                  <Icon name="trash" size={16} />
                </button>
              )}
            </span>
          </div>
        ))
      )}
      {!readOnly && (
        <div className="row" style={{ gap: 10, alignItems: "flex-end", marginTop: 10, flexWrap: "wrap" }}>
          <Field style={{ margin: 0, width: 170 }} label={t("taxRates.validFrom")}>
            <input
              type="month"
              value={draft.valid_from}
              onChange={(e) => setDraft({ ...draft, valid_from: e.target.value })}
            />
          </Field>
          <Field style={{ margin: 0, width: 110 }} label={t("taxRates.rate")}>
            <input
              type="number"
              step="0.01"
              min="0"
              max="100"
              value={draft.rate}
              onChange={(e) => setDraft({ ...draft, rate: e.target.value })}
            />
          </Field>
          <Field style={{ margin: 0, width: 230 }} label={t("taxRates.basis")}>
            <select value={draft.basis} onChange={(e) => setDraft({ ...draft, basis: e.target.value })}>
              <option value="ACCRUAL">{t("taxRates.basis_ACCRUAL")}</option>
              <option value="CASH">{t("taxRates.basis_CASH")}</option>
            </select>
          </Field>
          <button onClick={addRate}>{t("taxRates.add")}</button>
        </div>
      )}
      <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>{t("taxRates.basisHint")}</p>

      <h4 style={{ margin: "18px 0 2px" }}>{t("payrollSettings.title")}</h4>
      <div className="crow">
        <span className="k">
          {t("payrollSettings.prevDay")}
          <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{t("payrollSettings.prevDayHint")}</div>
        </span>
        {readOnly ? (
          <strong>{settings.payroll_prev_month_until_day ?? 31}</strong>
        ) : (
          <input
            type="number" min="0" max="31"
            value={settings.payroll_prev_month_until_day ?? ""}
            onChange={(e) => onSettings?.({ ...settings, payroll_prev_month_until_day: e.target.value })}
            onBlur={(e) => save("payroll_prev_month_until_day", e.target.value === "" ? 0 : Number(e.target.value))}
            style={{ width: 90, height: 34, textAlign: "right" }}
          />
        )}
      </div>
      <div className="crow">
        <span className="k">
          {t("payrollSettings.shareInMargin")}
          <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>{t("payrollSettings.shareInMarginHint")}</div>
        </span>
        {readOnly ? (
          <strong>{settings.master_share_in_margin ? t("payrollSettings.yes") : t("payrollSettings.no")}</strong>
        ) : (
          <input
            type="checkbox" style={{ width: 22, height: 22, minHeight: 0 }}
            checked={!!settings.master_share_in_margin}
            onChange={(e) => save("master_share_in_margin", e.target.checked)}
            aria-label={t("payrollSettings.shareInMargin")}
          />
        )}
      </div>
    </div>
  );
}
