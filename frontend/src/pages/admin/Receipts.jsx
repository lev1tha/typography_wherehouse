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
  const { toast } = useUI();
  const [rows, setRows] = useState([]);
  const [stats, setStats] = useState(null);
  const [method, setMethod] = useState("");
  const [pstatus, setPstatus] = useState("");
  const [search, setSearch] = useState("");
  const [advancingId, setAdvancingId] = useState(null);
  const [paying, setPaying] = useState(null);

  function load() {
    const params = {};
    if (method) params.payment_method = method;
    if (pstatus) params.payment_status = pstatus;
    if (search) params.search = search;
    api.get("/sales/receipts/", { params }).then((r) => setRows(r.data.results));
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
  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [method, pstatus, search]);

  const columns = [
    { key: "id", label: t("receipts.number"), render: (r) => String(r.id).slice(0, 8) },
    { key: "client_name", label: t("checkout.client"), render: (r) => r.client_name || "—" },
    { key: "cashier_name", label: t("receipts.cashier") },
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
    { key: "total_price", label: t("common.total"), render: (r) => `${r.total_price} сом` },
    {
      key: "debt",
      label: t("receipts.debt"),
      render: (r) =>
        Number(r.debt) > 0 ? (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            <span style={{ color: "var(--danger)", fontWeight: 600 }}>{r.debt} сом</span>
            <button
              className="secondary"
              style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
              onClick={(e) => { e.stopPropagation(); setPaying(r); }}
            >
              {t("receipts.pay")}
            </button>
          </div>
        ) : (
          <span className="muted">0</span>
        ),
    },
    {
      key: "created_at",
      label: t("receipts.date"),
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
      <DataTable columns={columns} rows={rows} />

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
