/**
 * «Полка остатков» (2026-10-11, D-200…D-205).
 *
 * После заказа остаются годные куски и детали — брак, лишние детали, обрезки.
 * По учёту они уже «проданы» (материал ушёл в себестоимость заказа), а лежат
 * на полке и продаются дешевле. Здесь видно, что лежит и сколько, от какого
 * заказа, что продали (когда, каким чеком, за сколько) и что выбросили.
 * Склада и стоимости склада полка не касается.
 *
 * Смотрят все; кладут склад и админ; списывает (выбросили) только админ.
 */
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useAuth } from "../auth/AuthContext.jsx";
import { downloadFile } from "../utils/download.js";
import { formatDate, formatDateTime, formatMoney, formatNumber } from "../utils/format.js";
import { isCanceled, useLatest } from "../utils/latest.js";
import DataTable from "./DataTable.jsx";
import Field from "./Field.jsx";
import LoadError from "./LoadError.jsx";
import Modal from "./Modal.jsx";
import MonthPicker from "./MonthPicker.jsx";
import ShelfPutModal from "./ShelfPutModal.jsx";
import Tabs from "./Tabs.jsx";
import { useUI } from "./UIProvider.jsx";

const VIEWS = ["list", "summary", "sold"];
const STATUSES = ["ON_SHELF", "PARTIAL", "SOLD", "WRITTEN_OFF"];
const TONE = { ON_SHELF: "ok", PARTIAL: "amber", SOLD: "blue", WRITTEN_OFF: "red" };
const qty = (v, max = 3) => formatNumber(v, { max });
const pad = (n) => String(n).padStart(2, "0");

// Период «Продано»: месяц — с первого по последний день, без месяца — год.
function periodRange({ year, month }) {
  if (!month) return { date_from: `${year}-01-01`, date_to: `${year}-12-31` };
  const last = new Date(year, month, 0).getDate();
  return { date_from: `${year}-${pad(month)}-01`, date_to: `${year}-${pad(month)}-${pad(last)}` };
}

export function StatusBadgeShelf({ status }) {
  const { t } = useTranslation();
  return <span className={`badge ${TONE[status] || ""}`}>{t(`shelf.status.${status}`)}</span>;
}

/** Сколько лежит: «2 из 3 шт» и площадь или метры. */
function leftText(lo, t) {
  const parts = [t("shelf.leftOf", { left: lo.pieces_left, total: lo.pieces })];
  if (lo.measure === "SQM" && Number(lo.area_left) > 0) parts.push(`${qty(lo.area_left)} ${t("unit.SQM")}`);
  if (lo.measure === "METER" && Number(lo.metres_left) > 0) parts.push(`${qty(lo.metres_left)} ${t("unit.METER")}`);
  return parts.join(" · ");
}

export default function Shelf({ embedded = false }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const { isAdmin, isAccountant } = useAuth();
  const canPut = !isAccountant;
  const [view, setView] = useState("list");
  const [materials, setMaterials] = useState([]);
  const [filters, setFilters] = useState({ q: "", material: "", measure: "", status: "", age: "", w: "", l: "" });
  const [rows, setRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const now = new Date();
  const [period, setPeriod] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 });
  const [sold, setSold] = useState(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [putting, setPutting] = useState(false);
  const [writingOff, setWritingOff] = useState(null);
  const [opened, setOpened] = useState(null);
  const next = useLatest();

  const params = useMemo(() => {
    const p = {};
    if (filters.q.trim()) p.q = filters.q.trim();
    if (filters.material) p.material = filters.material;
    if (filters.measure) p.measure = filters.measure;
    if (filters.status) p.status = filters.status;
    if (filters.age) p.age_min = filters.age;
    if (filters.w) p.min_width = filters.w.replace(",", ".");
    if (filters.l) p.min_length = filters.l.replace(",", ".");
    return p;
  }, [filters]);
  const filtered = Object.keys(params).length > 0;

  function load() {
    setLoading(true);
    const signal = next();
    const lists = Promise.all([
      api.get("/warehouse/leftovers/", { params: { ...params, page_size: 500 }, signal }),
      api.get("/warehouse/leftovers/summary/", { params, signal }),
      api.get("/warehouse/leftovers/sales/", {
        params: { ...periodRange(period), ...(filters.material ? { material: filters.material } : {}) },
        signal,
      }),
    ]);
    lists
      .then(([list, sum, history]) => {
        setRows(list.data.results ?? list.data);
        setSummary(sum.data);
        setSold(history.data);
        setFailed(false);
      })
      .catch((e) => {
        if (isCanceled(e)) return;
        setFailed(true);
        toast(apiError(e, t("common.loadFailed")), "error");
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params, period]);

  useEffect(() => {
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results ?? r.data))
      .catch(() => setMaterials([]));
  }, []);

  const set = (key) => (e) => setFilters((f) => ({ ...f, [key]: e.target.value }));
  const reset = () => setFilters({ q: "", material: "", measure: "", status: "", age: "", w: "", l: "" });

  const listColumns = [
    {
      key: "label",
      label: t("shelf.colPiece"),
      render: (lo) => (
        <span>
          <strong>{lo.material_name}</strong>
          {lo.size_text ? <span className="muted"> · {lo.size_text}</span> : null}
          {lo.site_name ? <span className="muted"> · {lo.site_name}</span> : null}
        </span>
      ),
    },
    { key: "left", label: t("shelf.colLeft"), render: (lo) => leftText(lo, t) },
    { key: "status", label: t("shelf.colStatus"), render: (lo) => <StatusBadgeShelf status={lo.status} /> },
    {
      key: "sold_amount",
      label: t("shelf.colSoldSum"),
      render: (lo) => (Number(lo.sold_amount) > 0 ? formatMoney(lo.sold_amount) : "—"),
    },
    {
      key: "order",
      label: t("shelf.colOrder"),
      render: (lo) => (lo.source_receipt_number ? `№${lo.source_receipt_number}` : "—"),
    },
    { key: "note", label: t("shelf.colNote"), render: (lo) => lo.note || "—" },
    {
      key: "age",
      label: t("shelf.colAge"),
      render: (lo) => (
        <span title={formatDateTime(lo.created_at)}>
          {t("shelf.days", { n: lo.age_days })}
          {lo.created_by_name ? <span className="muted"> · {lo.created_by_name}</span> : null}
        </span>
      ),
    },
    {
      key: "actions",
      label: "",
      render: (lo) => (
        <div className="row-actions">
          <button type="button" className="ghost row-btn" onClick={() => setOpened(lo)}>
            {t("shelf.open")}
          </button>
          {isAdmin && lo.pieces_left > 0 && (
            <button type="button" className="ghost row-btn row-danger" onClick={() => setWritingOff(lo)}>
              {t("shelf.writeOff")}
            </button>
          )}
        </div>
      ),
    },
  ];

  const summaryColumns = [
    { key: "material_name", label: t("shelf.colMaterial"), render: (r) => <strong>{r.material_name}</strong> },
    { key: "positions", label: t("shelf.colPositions") },
    { key: "pieces", label: t("shelf.colPieces") },
    { key: "area", label: t("shelf.colAreaSqm"), render: (r) => (Number(r.area) > 0 ? qty(r.area) : "—") },
    { key: "metres", label: t("shelf.colMetres"), render: (r) => (Number(r.metres) > 0 ? qty(r.metres) : "—") },
    {
      key: "oldest",
      label: t("shelf.colOldest"),
      render: (r) => `${formatDate(r.oldest)} · ${t("shelf.days", { n: r.oldest_days })}`,
    },
  ];

  const soldColumns = [
    { key: "sold_at", label: t("shelf.colSoldAt"), render: (r) => formatDate(r.sold_at) },
    { key: "order_number", label: t("shelf.colReceipt"), render: (r) => (r.order_number ? `№${r.order_number}` : "—") },
    { key: "label", label: t("shelf.colPiece") },
    { key: "pieces", label: t("shelf.colPieces") },
    { key: "price", label: t("shelf.colPrice"), render: (r) => formatMoney(r.price) },
    {
      key: "total",
      label: t("shelf.colSum"),
      render: (r) => (
        <span>
          {formatMoney(r.total)}
          {r.is_returned && (
            <span className="badge warn" style={{ marginLeft: 6 }}>
              {t("shelf.returnedOn", { date: formatDate(r.returned_at) })}
            </span>
          )}
        </span>
      ),
    },
  ];

  const totals = sold?.totals;

  return (
    <>
      {!embedded && <h1>{t("shelf.title")}</h1>}
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("shelf.hint")}</p>

      <div className="toolbar shelf-toolbar">
        <input
          className="search"
          type="search"
          aria-label={t("common.search")}
          placeholder={t("shelf.searchPh")}
          value={filters.q}
          onChange={set("q")}
        />
        <select aria-label={t("shelf.colMaterial")} value={filters.material} onChange={set("material")}>
          <option value="">{t("shelf.allMaterials")}</option>
          {materials.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
        </select>
        <select aria-label={t("shelf.measure")} value={filters.measure} onChange={set("measure")}>
          <option value="">{t("shelf.allMeasures")}</option>
          {["SQM", "METER", "PIECE"].map((m) => <option key={m} value={m}>{t(`shelf.measure${m}`)}</option>)}
        </select>
        <select aria-label={t("shelf.colStatus")} value={filters.status} onChange={set("status")}>
          <option value="">{t("shelf.statusAvailable")}</option>
          {STATUSES.map((s) => <option key={s} value={s}>{t(`shelf.status.${s}`)}</option>)}
          <option value="ALL">{t("shelf.statusAll")}</option>
        </select>
        <select aria-label={t("shelf.colAge")} value={filters.age} onChange={set("age")}>
          <option value="">{t("shelf.ageAll")}</option>
          {[30, 90, 180].map((d) => <option key={d} value={d}>{t("shelf.ageOver", { n: d })}</option>)}
        </select>
        <div className="shelf-size" role="group" aria-label={t("shelf.sizeSearch")}>
          <span className="muted">{t("shelf.sizeSearch")}</span>
          <input inputMode="decimal" placeholder={t("shelf.sizeW")} aria-label={t("shelf.sizeW")}
                 value={filters.w} onChange={set("w")} />
          <span aria-hidden="true">×</span>
          <input inputMode="decimal" placeholder={t("shelf.sizeL")} aria-label={t("shelf.sizeL")}
                 value={filters.l} onChange={set("l")} />
        </div>
      </div>
      <div className="toolbar">
        {canPut && <button type="button" onClick={() => setPutting(true)}>{t("shelf.put")}</button>}
        <button
          type="button"
          className="secondary"
          onClick={() => downloadFile("/warehouse/leftovers/export/", params, "polka-ostatkov.csv")}
        >
          {t("stock2.toExcel")}
        </button>
      </div>

      <Tabs
        id="shelf"
        panel={false}
        label={t("shelf.title")}
        value={view}
        onChange={setView}
        tabs={VIEWS.map((key) => ({ key, label: t(`shelf.tab_${key}`) }))}
      />

      {failed && !rows.length && <LoadError onRetry={load} />}

      {view === "list" && (
        <DataTable
          columns={listColumns}
          rows={rows}
          empty={loading ? t("common.loading") : t("shelf.empty")}
          filtered={filtered}
          onReset={reset}
          onRowClick={setOpened}
        />
      )}

      {view === "summary" && summary && (
        <>
          <p className="muted" style={{ fontSize: 13 }}>{t("shelf.summaryHint")}</p>
          <DataTable
            columns={summaryColumns}
            rows={summary.rows}
            rowKey="material"
            empty={loading ? t("common.loading") : t("shelf.empty")}
          />
          {summary.rows.length > 0 && (
            <p style={{ marginTop: 10 }}>
              <strong>{t("shelf.total")}:</strong>{" "}
              {t("shelf.totalLine", {
                pieces: summary.totals.pieces,
                area: qty(summary.totals.area),
                metres: qty(summary.totals.metres),
              })}
            </p>
          )}
        </>
      )}

      {view === "sold" && (
        <>
          <div className="toolbar" style={{ alignItems: "flex-end" }}>
            <MonthPicker value={period} onChange={setPeriod} />
          </div>
          {totals && (
            <div className="shelf-totals">
              <div className="card">
                <span className="muted">{t("shelf.soldTotal")}</span>
                <strong>{formatMoney(totals.sold)}</strong>
              </div>
              <div className="card">
                <span className="muted">{t("shelf.returnedTotal")}</span>
                <strong>{formatMoney(totals.returned)}</strong>
              </div>
              <div className="card">
                <span className="muted">{t("shelf.netTotal")}</span>
                <strong>{formatMoney(totals.net)}</strong>
                <span className="muted">{t("shelf.piecesN", { n: totals.pieces })}</span>
              </div>
            </div>
          )}
          {sold?.materials?.length > 0 && (
            <p className="muted" style={{ fontSize: 13 }}>
              {sold.materials.map((m) => `${m.name}: ${formatMoney(m.revenue)} (${t("shelf.piecesN", { n: m.pieces })})`).join(" · ")}
            </p>
          )}
          <DataTable
            columns={soldColumns}
            rows={sold?.rows || []}
            empty={loading ? t("common.loading") : t("shelf.emptySold")}
          />
        </>
      )}

      {putting && (
        <ShelfPutModal
          onClose={() => setPutting(false)}
          onDone={() => {
            setPutting(false);
            load();
          }}
        />
      )}
      {writingOff && (
        <ShelfWriteOffModal
          leftover={writingOff}
          onClose={() => setWritingOff(null)}
          onDone={() => {
            setWritingOff(null);
            setOpened(null);
            load();
          }}
        />
      )}
      {opened && !writingOff && (
        <ShelfCardModal
          leftover={opened}
          canWriteOff={isAdmin}
          onWriteOff={(lo) => setWritingOff(lo)}
          onClose={() => setOpened(null)}
        />
      )}
    </>
  );
}

/** Списать с полки (выбросили) — админ, с причиной; уходит в журнал. */
function ShelfWriteOffModal({ leftover, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      await api.post(`/warehouse/leftovers/${leftover.id}/write-off/`, { reason: reason.trim() });
      toast(t("shelf.writeOffDone"));
      onDone();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("shelf.writeOffTitle", { label: leftover.label })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button className="danger" onClick={submit} disabled={busy || !reason.trim()}>
            {t("shelf.writeOff")}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        {t("shelf.writeOffHint", { n: leftover.pieces_left })}
      </p>
      <Field label={t("shelf.reason")} required>
        <input autoFocus value={reason} placeholder={t("shelf.reasonPh")} onChange={(e) => setReason(e.target.value)} />
      </Field>
    </Modal>
  );
}

/** Карточка остатка: откуда, кто положил, и история — по какому чеку и за сколько. */
function ShelfCardModal({ leftover, canWriteOff, onWriteOff, onClose }) {
  const { t } = useTranslation();
  const [data, setData] = useState(null);

  useEffect(() => {
    api.get(`/warehouse/leftovers/${leftover.id}/`).then((r) => setData(r.data)).catch(() => setData(leftover));
  }, [leftover]);

  const lo = data || leftover;
  return (
    <Modal
      title={lo.label}
      onClose={onClose}
      footer={
        <>
          {canWriteOff && lo.pieces_left > 0 && (
            <button className="danger" onClick={() => onWriteOff(lo)}>{t("shelf.writeOff")}</button>
          )}
          <button className="secondary" onClick={onClose}>{t("common.close")}</button>
        </>
      }
    >
      <div className="crow"><span className="k">{t("shelf.colStatus")}</span><StatusBadgeShelf status={lo.status} /></div>
      <div className="crow"><span className="k">{t("shelf.colLeft")}</span><span>{leftText(lo, t)}</span></div>
      {lo.source_receipt_number && (
        <div className="crow"><span className="k">{t("shelf.colOrder")}</span><span>№{lo.source_receipt_number}</span></div>
      )}
      {lo.site_name && <div className="crow"><span className="k">{t("shelf.site")}</span><span>{lo.site_name}</span></div>}
      {lo.note && <div className="crow"><span className="k">{t("shelf.colNote")}</span><span>{lo.note}</span></div>}
      <div className="crow">
        <span className="k">{t("shelf.putBy")}</span>
        <span>{[lo.created_by_name, formatDateTime(lo.created_at)].filter(Boolean).join(" · ")}</span>
      </div>
      {lo.written_off_pieces > 0 && (
        <div className="crow">
          <span className="k">{t("shelf.status.WRITTEN_OFF")}</span>
          <span>
            {t("shelf.piecesN", { n: lo.written_off_pieces })}
            {lo.written_off_at ? ` · ${formatDateTime(lo.written_off_at)}` : ""}
            {lo.written_off_by_name ? ` · ${lo.written_off_by_name}` : ""}
            {lo.write_off_reason ? ` · ${lo.write_off_reason}` : ""}
          </span>
        </div>
      )}
      <h4 style={{ margin: "14px 0 4px" }}>{t("shelf.history")}</h4>
      {!data ? (
        <p className="muted">{t("common.loading")}</p>
      ) : !data.sales?.length ? (
        <p className="muted">{t("shelf.noSales")}</p>
      ) : (
        data.sales.map((s) => (
          <div className="crow" key={s.id}>
            <span>
              {formatDate(s.sold_at)} · {s.order_number ? `№${s.order_number}` : "—"}
              {s.is_returned && (
                <span className="badge warn" style={{ marginLeft: 6 }}>
                  {t("shelf.returnedOn", { date: formatDate(s.returned_at) })}
                </span>
              )}
            </span>
            <span>
              {t("shelf.piecesN", { n: s.pieces })} × {formatMoney(s.price)} = <strong>{formatMoney(s.total)}</strong>
            </span>
          </div>
        ))
      )}
    </Modal>
  );
}
