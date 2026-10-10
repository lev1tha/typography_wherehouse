import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { formatDate, formatMoney, formatNumber } from "../utils/format.js";
import LotCorrectionModal from "./LotCorrectionModal.jsx";
import Modal from "./Modal.jsx";
import Pager from "./Pager.jsx";

// Все партии материала — и пустые тоже: опечатку чаще всего находят, когда
// партия уже распродана. Отсюда — «Исправить приход» (только администратор).

const PAGE = 10;

/** Партия → то, что ждёт окно исправления. */
export function lotForCorrection(r, material, t) {
  const form = r.form === "PIECE" ? "PIECE" : r.form;
  const quantity = r.initial_area;
  return {
    roll: r.id,
    form,
    width: r.width,
    height: r.height,
    length: r.length,
    sheet_count: r.sheet_count,
    quantity,
    cost: r.purchase_cost,
    unit: form === "PIECE" ? t(`unit.${material.unit}`) : t("unit.SQM"),
    title: `${material.name} · ${r.code || `№${r.id}`} · ${r.dimensions_label} · ${formatDate(r.received_at)}`,
  };
}

export default function LotsModal({ material, onClose, onChanged }) {
  const { t } = useTranslation();
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [page, setPage] = useState(1);
  const [error, setError] = useState("");
  const [fixing, setFixing] = useState(null);

  const load = useCallback(() => {
    api
      .get("/warehouse/rolls/", {
        params: { material: material.id, ordering: "-received_at", page, page_size: PAGE },
      })
      .then((r) => {
        setRows(r.data.results ?? r.data);
        setCount(r.data.count ?? (r.data.results ?? r.data).length);
        setError("");
      })
      .catch((e) => setError(apiError(e, t("common.error"))));
  }, [material.id, page, t]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <Modal wide title={t("lotFix.lotsTitle", { name: material.name })} onClose={onClose}>
        {error && <p className="field-error" role="alert">{error}</p>}
        {!error && rows.length === 0 && <p className="muted">{t("lotFix.noLots")}</p>}
        {rows.length > 0 && (
          <div className="table-scroll">
            <table className="table plain-table">
              <thead>
                <tr>
                  <th>{t("lotFix.date")}</th>
                  <th>{t("lotFix.lot")}</th>
                  <th>{t("lotFix.left")}</th>
                  <th>{t("lotFix.lotCost")}</th>
                  <th>{t("lotFix.supply")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>{formatDate(r.received_at)}</td>
                    <td>
                      <strong>{r.code || `№${r.id}`}</strong>{" "}
                      <span className="muted">{r.dimensions_label}</span>
                    </td>
                    <td>
                      {formatNumber(r.remaining_area, { max: 2 })} / {formatNumber(r.initial_area, { max: 2 })}{" "}
                      <span className="muted">{r.form === "PIECE" ? t(`unit.${material.unit}`) : t("unit.SQM")}</span>
                    </td>
                    <td>{formatMoney(r.purchase_cost, { fraction: 2 })}</td>
                    <td className="muted">{r.supply_number || "—"}</td>
                    <td>
                      <button className="secondary row-btn" onClick={() => setFixing(r)}>
                        {t("lotFix.button")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Pager page={page} count={count} pageSize={PAGE} onPage={setPage} />
      </Modal>
      {fixing && (
        <LotCorrectionModal
          lot={lotForCorrection(fixing, material, t)}
          onClose={() => setFixing(null)}
          onDone={() => {
            load();
            onChanged?.();
          }}
        />
      )}
    </>
  );
}
