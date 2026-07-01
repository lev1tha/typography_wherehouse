import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import Modal from "../../components/Modal.jsx";
import { FulfillmentBadge, PaymentBadge } from "../../components/StatusBadge.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

export default function CustomerOrders() {
  const { t } = useTranslation();
  const { user } = useAuth();
  const [orders, setOrders] = useState(null);
  const [showPay, setShowPay] = useState(false);

  useEffect(() => {
    api
      .get("/customer/orders/")
      .then((r) => setOrders(r.data))
      .catch(() => setOrders([]));
  }, []);

  if (orders === null) return <p className="muted">{t("common.loading")}</p>;

  const totalDebt = orders.reduce((s, o) => s + Number(o.debt), 0);

  return (
    <>
      <h1>{t("myOrders.title")}</h1>
      <p className="muted">{t("myOrders.hello", { name: user?.name || "" })}</p>

      <div className="stat-grid" style={{ margin: "12px 0 18px" }}>
        <div className="stat">
          <div className="label">{t("myOrders.count")}</div>
          <div className="value">{orders.length}</div>
        </div>
        <div className="stat">
          <div className="label">{t("myOrders.totalDebt")}</div>
          <div className="value" style={{ color: totalDebt > 0 ? "var(--danger)" : "var(--ok)" }}>
            {som(totalDebt)}
          </div>
          {totalDebt > 0 && (
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>{t("myOrders.debtHint")}</div>
          )}
        </div>
      </div>

      {totalDebt > 0 && (
        <button
          className="secondary"
          style={{ width: "auto", marginBottom: 18 }}
          onClick={() => setShowPay(true)}
        >
          {t("myOrders.howToPay")}
        </button>
      )}

      {orders.length === 0 ? (
        <div className="empty-state">{t("myOrders.empty")}</div>
      ) : (
        <div className="cards" style={{ display: "flex" }}>
          {orders.map((o) => (
            <div className="data-card" key={o.id}>
              <div className="crow">
                <strong>№{o.order_number}</strong>
                <span className="muted">{new Date(o.created_at).toLocaleDateString("ru-RU")}</span>
              </div>
              <div style={{ margin: "8px 0" }}>
                {o.items.map((it, i) => (
                  <div className="crow" key={i}>
                    <span className="k">
                      {it.title} × {Number(it.quantity)}
                    </span>
                    <span>{som(it.line_total)}</span>
                  </div>
                ))}
              </div>
              <div className="crow">
                <span className="k">{t("common.total")}</span>
                <strong>{som(o.total_price)}</strong>
              </div>
              {Number(o.debt) > 0 && (
                <div className="crow">
                  <span className="k">{t("receipts.debt")}</span>
                  <strong style={{ color: "var(--danger)" }}>{som(o.debt)}</strong>
                </div>
              )}
              <div className="crow" style={{ marginTop: 6, gap: 8, justifyContent: "flex-start", flexWrap: "wrap" }}>
                <span className="k">{t("myOrders.payLabel")}</span>
                <PaymentBadge status={o.payment_status} />
                <span className="k" style={{ marginLeft: 6 }}>{t("myOrders.fulfillLabel")}</span>
                <FulfillmentBadge status={o.fulfillment_status} />
              </div>
            </div>
          ))}
        </div>
      )}

      {showPay && (
        <Modal
          title={t("myOrders.payTitle")}
          onClose={() => setShowPay(false)}
          footer={<button onClick={() => setShowPay(false)}>{t("common.close")}</button>}
        >
          <div className="pos-total" style={{ fontSize: 18, marginBottom: 8 }}>
            <span>{t("myOrders.totalDebt")}</span>
            <span style={{ color: "var(--danger)" }}>{som(totalDebt)}</span>
          </div>
          <ol style={{ paddingLeft: 18, lineHeight: 1.8, margin: "8px 0" }}>
            <li>{t("myOrders.payStep1")}</li>
            <li>{t("myOrders.payStep2")}</li>
          </ol>
          <p className="muted">{t("myOrders.payNote")}</p>
        </Modal>
      )}
    </>
  );
}
