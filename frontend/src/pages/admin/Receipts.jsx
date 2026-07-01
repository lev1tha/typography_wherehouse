import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import PayDebtModal from "../../components/PayDebtModal.jsx";
import { FulfillmentBadge, PaymentBadge } from "../../components/StatusBadge.jsx";
import { useUI } from "../../components/UIProvider.jsx";

function ReceiptsTab() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [stats, setStats] = useState(null);
  const [method, setMethod] = useState("");
  const [pstatus, setPstatus] = useState("");
  const [search, setSearch] = useState("");
  const [advancingId, setAdvancingId] = useState(null);
  const [paying, setPaying] = useState(null);
  const [sort, setSort] = useState({ key: "_debt", dir: "desc" });

  function orderingParam() {
    // Вторичная сортировка по дате (кроме случая, когда уже сортируем по дате).
    const tail = sort.key !== "created_at" ? ",-created_at" : "";
    return (sort.dir === "desc" ? "-" : "") + sort.key + tail;
  }

  function onSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" }));
  }

  function load() {
    const params = {};
    if (method) params.payment_method = method;
    if (pstatus) params.payment_status = pstatus;
    if (search) params.search = search;
    api.get("/sales/receipts/", { params: { ...params, ordering: orderingParam() } }).then((r) => setRows(r.data.results));
    api.get("/sales/receipts/stats/", { params }).then((r) => setStats(r.data));
  }

  const nextShort = (s) => (s === "PROCESSING" ? t("receipts.toReady") : t("receipts.toIssued"));

  async function advance(r, e) {
    e?.stopPropagation();
    const action = r.fulfillment_status === "PROCESSING" ? "mark-ready" : "mark-issued";
    setAdvancingId(r.id);
    try {
      await api.post(`/sales/receipts/${r.id}/${action}/`, {});
      load();
      toast(t("receipts.statusUpdated"));
    } catch {
      toast(t("common.error"), "error");
    } finally {
      setAdvancingId(null);
    }
  }

  async function undoPay(r, e) {
    e?.stopPropagation();
    if (!(await confirm(t("receipts.confirmUnpay")))) return;
    try {
      await api.post(`/sales/receipts/${r.id}/unpay/`, {});
      load();
      toast(t("receipts.unpayDone"));
    } catch (err) {
      toast(err.response?.data?.detail || t("common.error"), "error");
    }
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [method, pstatus, search, sort]);

  const columns = [
    { key: "order_number", label: t("receipts.number"), render: (r) => `№${r.order_number ?? "—"}` },
    { key: "client_name", label: t("checkout.client"), render: (r) => r.client_name || "—" },
    {
      key: "cashier_name",
      label: t("receipts.cashier"),
      render: (r) =>
        r.cashier_name ? (
          <span>
            {r.cashier_name}
            {r.cashier_role && <span className="muted"> · {r.cashier_role}</span>}
          </span>
        ) : (
          "—"
        ),
    },
    {
      key: "payment_method",
      label: t("receipts.method"),
      render: (r) => t(`checkout.${r.payment_method.toLowerCase()}`),
    },
    {
      key: "payment_status",
      label: t("receipts.status"),
      render: (r) => <PaymentBadge status={r.payment_status} />,
    },
    {
      key: "fulfillment",
      label: t("receipts.fulfillment"),
      render: (r) =>
        r.has_service ? (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            <FulfillmentBadge status={r.fulfillment_status} />
            {r.fulfillment_status !== "ISSUED" && (
              <button
                className="secondary"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
                disabled={advancingId === r.id}
                onClick={(e) => advance(r, e)}
                title={nextShort(r.fulfillment_status)}
              >
                → {nextShort(r.fulfillment_status)}
              </button>
            )}
          </div>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { key: "total_price", label: t("common.total"), sortKey: "total_price", render: (r) => `${r.total_price} сом` },
    {
      key: "debt",
      label: t("receipts.debt"),
      sortKey: "_debt",
      render: (r) => {
        const hasDebt = Number(r.debt) > 0;
        const canUndo =
          (r.payment_status === "PAID" || Number(r.amount_paid) > 0) &&
          !["REFUNDED", "PARTIALLY_REFUNDED"].includes(r.payment_status) &&
          r.status !== "CANCELLED";
        if (!hasDebt && !canUndo) return <span className="muted">0</span>;
        return (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            {hasDebt && <span style={{ color: "var(--danger)", fontWeight: 600 }}>{r.debt} сом</span>}
            {hasDebt && (
              <button
                className="secondary"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
                onClick={(e) => { e.stopPropagation(); setPaying(r); }}
              >
                {t("receipts.pay")}
              </button>
            )}
            {canUndo && (
              <button
                className="ghost"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap", color: "var(--ink-muted)" }}
                onClick={(e) => undoPay(r, e)}
                title={t("receipts.unpay")}
              >
                ↩ {t("receipts.unpayShort")}
              </button>
            )}
          </div>
        );
      },
    },
    {
      key: "created_at",
      label: t("receipts.date"),
      sortKey: "created_at",
      render: (r) => new Date(r.created_at).toLocaleString("ru-RU"),
    },
  ];

  return (
    <>
      {stats && (
        <div className="stat-grid" style={{ marginBottom: 16 }}>
          <div className="stat"><div className="label">{t("receipts.statTotal")}</div><div className="value">{stats.total}</div></div>
          <div className="stat"><div className="label">{t("receipts.statWorking")}</div><div className="value">{stats.working}</div></div>
          <div className="stat"><div className="label">{t("receipts.statReady")}</div><div className="value">{stats.ready}</div></div>
          <div className="stat">
            <div className="label">{t("receipts.debt")}</div>
            <div className="value" style={Number(stats.debt) > 0 ? { color: "var(--danger)" } : undefined}>
              {Math.round(Number(stats.debt)).toLocaleString("ru-RU")} сом
            </div>
          </div>
        </div>
      )}
      <div className="toolbar">
        <input
          className="search"
          placeholder={t("common.search")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={method} onChange={(e) => setMethod(e.target.value)}>
          <option value="">{t("receipts.method")}: {t("common.all")}</option>
          <option value="CASH">{t("checkout.cash")}</option>
          <option value="MBANK">{t("checkout.mbank")}</option>
          <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
          <option value="ONLINE">{t("checkout.online")}</option>
        </select>
        <select value={pstatus} onChange={(e) => setPstatus(e.target.value)}>
          <option value="">{t("receipts.status")}: {t("common.all")}</option>
          {["PENDING", "PAID", "REFUNDED", "PARTIALLY_REFUNDED"].map((s) => (
            <option key={s} value={s}>
              {t(`payment.${s}`)}
            </option>
          ))}
        </select>
      </div>
      <DataTable columns={columns} rows={rows} sort={sort} onSort={onSort} />

      {paying && (
        <PayDebtModal
          receipt={paying}
          onClose={() => setPaying(null)}
          onPaid={() => { setPaying(null); load(); }}
        />
      )}
    </>
  );
}

function auditIcon(action = "") {
  const a = action.toLowerCase();
  if (a.includes("вход")) return "key";
  if (a.includes("возврат")) return "undo";
  if (a.includes("цен")) return "tag";
  if (a.includes("оформлен чек") || a.includes("чек")) return "receipt";
  if (a.includes("инвентар")) return "clipboard";
  if (a.includes("списан")) return "trash";
  if (a.includes("поступлен")) return "inbox";
  if (a.includes("готов") || a.includes("выдан")) return "check-circle";
  if (a.includes("дозаказ")) return "plus-circle";
  if (a.includes("реферер")) return "shuffle";
  return "dot";
}

function AuditTab() {
  const { t } = useTranslation();
  const [rows, setRows] = useState([]);
  useEffect(() => {
    api.get("/audit/logs/").then((r) => setRows(r.data.results));
  }, []);

  if (!rows.length) {
    return (
      <div className="empty-state">
        <Icon name="archive" size={40} className="es-icon" />
        {t("common.empty")}
      </div>
    );
  }

  return (
    <div className="feed">
      {rows.map((r) => (
        <div className="feed-item" key={r.id}>
          <div className="feed-icon"><Icon name={auditIcon(r.action)} size={17} /></div>
          <div className="feed-body">
            <div className="feed-action">{r.action}</div>
            <div className="feed-meta">
              {r.username || "—"} · {new Date(r.created_at).toLocaleString("ru-RU")}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function Receipts() {
  const { t } = useTranslation();
  const [tab, setTab] = useState("receipts");

  return (
    <>
      <h1>{t("receipts.title")}</h1>
      <div className="tabs">
        <button
          className={tab === "receipts" ? "active" : ""}
          onClick={() => setTab("receipts")}
        >
          {t("receipts.title")}
        </button>
        <button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>
          {t("nav.audit")}
        </button>
      </div>
      {tab === "receipts" ? <ReceiptsTab /> : <AuditTab />}
    </>
  );
}
