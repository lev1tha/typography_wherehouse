import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import AddToOrderModal from "../../components/AddToOrderModal.jsx";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import PayDebtModal from "../../components/PayDebtModal.jsx";
import { FulfillmentBadge, PaymentBadge } from "../../components/StatusBadge.jsx";
import { useUI } from "../../components/UIProvider.jsx";

export default function StoreReceipts() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState([]);
  const [stats, setStats] = useState(null);
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [advancingId, setAdvancingId] = useState(null);
  const [paying, setPaying] = useState(null);

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

  function load() {
    const params = search ? { search } : {};
    api.get("/sales/receipts/", { params }).then((r) => setRows(r.data.results));
    api.get("/sales/receipts/stats/", { params }).then((r) => setStats(r.data));
  }
  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  async function refund() {
    if (!(await confirm(t("receipts.confirmRefund")))) return;
    setBusy(true);
    try {
      const { data } = await api.post(`/sales/receipts/${open.id}/refund/`, {});
      setOpen(data);
      load();
      toast(t("receipts.refundDone"));
    } catch {
      toast(t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  async function setFulfillment(action) {
    setBusy(true);
    try {
      const { data } = await api.post(`/sales/receipts/${open.id}/${action}/`, {});
      setOpen(data);
      load();
      toast(t("receipts.statusUpdated"));
    } catch {
      toast(t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  const columns = [
    { key: "id", label: t("receipts.number"), render: (r) => String(r.id).slice(0, 8) },
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
    {
      key: "actions",
      label: t("common.actions"),
      render: (r) => (
        <button className="ghost" onClick={() => setOpen(r)} aria-label={t("common.edit")}>
          <Icon name="arrow-right" size={18} />
        </button>
      ),
    },
  ];

  const canRefund = open && !["REFUNDED", "CANCELLED"].includes(open.payment_status) && open.status !== "CANCELLED";
  const canEdit = open && open.payment_status !== "REFUNDED" && open.status !== "CANCELLED";

  return (
    <>
      <h1>{t("receipts.title")}</h1>
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
          placeholder={`${t("common.search")} (${t("receipts.number")})`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <DataTable columns={columns} rows={rows} />

      {open && (
        <Modal
          title={`${t("checkout.receipt")} № ${String(open.id).slice(0, 8)}`}
          onClose={() => setOpen(null)}
          footer={
            <>
              {Number(open.debt) > 0 && (
                <button onClick={() => setPaying(open)} disabled={busy}>
                  {t("receipts.acceptPayment")}
                </button>
              )}
              {canEdit && (
                <button className="secondary" onClick={() => setAdding(true)} disabled={busy}>
                  + {t("receipts.addBtn")}
                </button>
              )}
              {open.has_service && open.fulfillment_status === "PROCESSING" && (
                <button className="secondary" onClick={() => setFulfillment("mark-ready")} disabled={busy}>
                  {t("receipts.markReady")}
                </button>
              )}
              {open.has_service && open.fulfillment_status === "READY" && (
                <button className="secondary" onClick={() => setFulfillment("mark-issued")} disabled={busy}>
                  {t("receipts.markIssued")}
                </button>
              )}
              {canRefund && (
                <button className="danger" onClick={refund} disabled={busy}>
                  {t("receipts.refund")}
                </button>
              )}
            </>
          }
        >
          {open.items.map((it) => (
            <div className="crow" key={it.id}>
              <span>
                {(it.type === "SERVICE" ? it.service_name : it.material_name)} × {it.quantity}
                {it.is_returned && (
                  <span className="badge warn" style={{ marginLeft: 6 }}>
                    {t("receipts.returned")}
                  </span>
                )}
              </span>
              <span>{it.line_total} сом</span>
            </div>
          ))}
          <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 8 }}>
            <strong>{t("common.total")}</strong>
            <strong>{open.total_price} сом</strong>
          </div>
          {Number(open.debt) > 0 && (
            <div className="crow">
              <span className="k">{t("receipts.paid")}</span>
              <span>{open.amount_paid} сом</span>
            </div>
          )}
          {Number(open.debt) > 0 && (
            <div className="crow">
              <span className="k">{t("receipts.debt")}</span>
              <strong style={{ color: "var(--danger)" }}>{open.debt} сом</strong>
            </div>
          )}
          <div className="crow">
            <span className="k">{t("receipts.status")}</span>
            <PaymentBadge status={open.payment_status} />
          </div>
          {open.has_service && (
            <div className="crow">
              <span className="k">{t("receipts.fulfillment")}</span>
              <FulfillmentBadge status={open.fulfillment_status} />
            </div>
          )}
        </Modal>
      )}

      {adding && open && (
        <AddToOrderModal
          receiptId={open.id}
          onClose={() => setAdding(false)}
          onAdded={(data) => {
            setOpen(data);
            setAdding(false);
            load();
          }}
        />
      )}

      {paying && (
        <PayDebtModal
          receipt={paying}
          onClose={() => setPaying(null)}
          onPaid={(data) => {
            setPaying(null);
            if (open && open.id === data.id) setOpen(data);
            load();
          }}
        />
      )}
    </>
  );
}
