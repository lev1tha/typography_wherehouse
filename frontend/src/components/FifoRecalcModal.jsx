/**
 * «Пересчитать себестоимость по FIFO» с даты (PNL-08, волна 2).
 *
 * Поставку внесли задним числом — продажи после её даты уже списали другую
 * партию по другой цене. Здесь: дата → предпросмотр (какие строки чеков и на
 * сколько меняют себестоимость, какие партии) → «Применить». Закрытый месяц —
 * отказ с перечнем.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { formatDate, formatMoney, formatNumber } from "../utils/format.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const monthStart = () => {
  const d = new Date();
  d.setDate(1);
  return d.toLocaleDateString("sv-SE");
};

export default function FifoRecalcModal({ material, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [since, setSince] = useState(monthStart());
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(mode) {
    setBusy(true);
    setError("");
    try {
      const r = await api.post(`/warehouse/fifo-recalc/${mode}/`, { material: material.id, since });
      if (mode === "apply") {
        toast(t("stock2.fifoDone", { count: r.data.items.length }));
        onDone?.();
        onClose();
        return;
      }
      setPlan(r.data);
    } catch (e) {
      setError(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  const lots = (list) => list.map((l) => `${l.lot}: ${formatNumber(l.area, { max: 2 })}`).join(", ");

  return (
    <Modal
      wide
      title={`${t("stock2.fifoTitle")}: ${material.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          {plan && plan.items.length > 0 ? (
            <button onClick={() => run("apply")} disabled={busy || plan.closed_months.length > 0}>
              {t("stock2.applyUpsert")}
            </button>
          ) : (
            <button onClick={() => run("preview")} disabled={busy || !since}>{t("stock2.previewUpsert")}</button>
          )}
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("stock2.fifoHint")}</p>
      <Field label={t("stock2.fifoSince")}>
        <input type="date" value={since} onChange={(e) => { setSince(e.target.value); setPlan(null); }} />
      </Field>
      {error && <p className="field-error" role="alert">{error}</p>}
      {plan && (plan.items.length === 0 ? (
        <p className="muted">{t("stock2.fifoNothing")}</p>
      ) : (
        <>
          <p style={{ fontSize: 13 }}>
            {t("stock2.fifoDelta")}: <strong>{formatMoney(plan.cogs_delta, { fraction: 2 })}</strong>
          </p>
          <div className="table-scroll">
          <table className="table plain-table">
            <thead>
              <tr>
                <th>{t("lotFix.date")}</th>
                <th>№</th>
                <th>{t("stock2.beforeAfter")}</th>
                <th>{t("stock2.sum")}</th>
              </tr>
            </thead>
            <tbody>
              {plan.items.map((it) => (
                <tr key={it.item}>
                  <td>{formatDate(it.date)}</td>
                  <td>{it.order_number ?? "—"}</td>
                  <td className="muted" style={{ fontSize: 12 }}>{lots(it.lots_before)} → {lots(it.lots_after)}</td>
                  <td>{formatMoney(it.cost_before, { fraction: 2 })} → <strong>{formatMoney(it.cost_after, { fraction: 2 })}</strong></td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </>
      ))}
    </Modal>
  );
}
