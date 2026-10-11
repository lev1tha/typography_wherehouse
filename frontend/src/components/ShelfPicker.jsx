/**
 * Касса → «Отходы» → «С полки» (D-201, D-202): найти кусок на полке остатков,
 * сколько кусков продаём и цена за кусок — ВРУЧНУЮ: каталожной цены у остатка
 * нет, подсказки тоже нет. Склад не двигается — кусок уже списан заказом.
 *
 * Состояние живёт в окне позиции кассы (`cut`): `leftover` — выбранный остаток,
 * `pieces`, `rate`, `note`. Сколько уже лежит в корзине с этого остатка —
 * `inCart`: столько же второй строкой не продать.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { formatMoney } from "../utils/format.js";
import { isCanceled, useLatest } from "../utils/latest.js";
import Field from "./Field.jsx";

const ceilSom = (v) => Math.max(0, Math.ceil((Number(v) || 0) - 1e-6));

/** Сколько кусков можно продать с выбранного остатка с учётом корзины. */
export function shelfAvailable(cut, inCart = {}) {
  const lo = cut?.leftover;
  if (!lo) return 0;
  return Math.max(0, Number(lo.pieces_left) - (inCart[lo.id] || 0));
}

export default function ShelfPicker({ cut, setCut, inCart = {} }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [w, setW] = useState("");
  const [l, setL] = useState("");
  const [rows, setRows] = useState(null);
  const next = useLatest();

  useEffect(() => {
    const id = setTimeout(() => {
      const params = { page_size: 50 };
      if (q.trim()) params.q = q.trim();
      if (w) params.min_width = w.replace(",", ".");
      if (l) params.min_length = l.replace(",", ".");
      api
        .get("/warehouse/leftovers/", { params, signal: next() })
        .then((r) => setRows(r.data.results ?? r.data))
        .catch((e) => {
          if (!isCanceled(e)) setRows([]);
        });
    }, 250);
    return () => clearTimeout(id);
  }, [q, w, l, next]);

  const picked = cut.leftover;
  const available = shelfAvailable(cut, inCart);
  const pieces = Number(cut.pieces) || 0;
  const rate = Number(cut.rate) || 0;

  return (
    <>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("shelf.pickHint")}</p>
      <div className="row">
        <Field className="grow" label={t("shelf.pickSearch")}>
          <input type="search" value={q} placeholder={t("shelf.searchPh")} onChange={(e) => setQ(e.target.value)} autoFocus />
        </Field>
      </div>
      <div className="shelf-size" role="group" aria-label={t("shelf.sizeSearch")}>
        <span className="muted">{t("shelf.sizeSearch")}</span>
        <input inputMode="decimal" placeholder={t("shelf.sizeW")} aria-label={t("shelf.sizeW")} value={w} onChange={(e) => setW(e.target.value)} />
        <span aria-hidden="true">×</span>
        <input inputMode="decimal" placeholder={t("shelf.sizeL")} aria-label={t("shelf.sizeL")} value={l} onChange={(e) => setL(e.target.value)} />
      </div>
      <div className="shelf-pick" role="listbox" aria-label={t("shelf.title")}>
        {rows === null ? (
          <p className="muted">{t("common.loading")}</p>
        ) : rows.length === 0 ? (
          <p className="muted">{t("shelf.pickNone")}</p>
        ) : (
          rows.map((lo) => {
            const active = picked?.id === lo.id;
            const left = Number(lo.pieces_left) - (inCart[lo.id] || 0);
            return (
              <button
                key={lo.id}
                type="button"
                role="option"
                aria-selected={active}
                className={`shelf-pick-item${active ? " active" : ""}`}
                disabled={left <= 0}
                onClick={() => setCut({ ...cut, leftover: lo, pieces: "1" })}
              >
                <strong>{lo.material_name}</strong>
                {lo.size_text ? <span> · {lo.size_text}</span> : null}
                <span className="muted">
                  {" · "}{t("shelf.pickLeft", { n: lo.pieces_left })}
                  {inCart[lo.id] ? ` · ${t("shelf.pickInCart", { n: inCart[lo.id] })}` : ""}
                  {" · "}{t("shelf.days", { n: lo.age_days })}
                  {lo.source_receipt_number ? ` · №${lo.source_receipt_number}` : ""}
                  {lo.note ? ` · ${lo.note}` : ""}
                </span>
              </button>
            );
          })
        )}
      </div>
      {picked && (
        <>
          <div className="row">
            <Field className="grow" label={`${t("shelf.pickPieces")} (≤ ${available})`} required>
              <input
                type="number" inputMode="numeric" min="1" max={available || undefined} step="1"
                value={cut.pieces ?? ""}
                onChange={(e) => setCut({ ...cut, pieces: e.target.value })}
              />
            </Field>
            <Field className="grow" label={t("shelf.pickRate")} required>
              <input
                type="number" inputMode="decimal" step="any" min="0"
                value={cut.rate ?? ""}
                onChange={(e) => setCut({ ...cut, rate: e.target.value })}
              />
            </Field>
          </div>
          {!(rate > 0) && (
            <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "-6px 0 8px" }}>{t("shelf.pickNeedRate")}</p>
          )}
          <Field label={t("checkout.wasteNote")}>
            <input value={cut.note ?? ""} onChange={(e) => setCut({ ...cut, note: e.target.value })} />
          </Field>
          {pieces > 0 && rate > 0 && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
              <div className="crow">
                <span className="k">{picked.label}</span>
                <span>{rate} × {pieces} = {ceilSom(rate * pieces)}</span>
              </div>
              <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 6 }}>
                <strong>{t("common.total")}</strong>
                <strong style={{ fontSize: 18 }}>{formatMoney(ceilSom(rate * pieces))}</strong>
              </div>
            </div>
          )}
        </>
      )}
    </>
  );
}
