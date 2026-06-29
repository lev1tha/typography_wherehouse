import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import api from "../../api/api.js";
import Icon from "../../components/Icon.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

function Action({ to, icon, title, subtitle }) {
  return (
    <Link
      to={to}
      className="card"
      style={{ display: "flex", alignItems: "center", gap: 14, textDecoration: "none", color: "inherit" }}
    >
      <span
        style={{
          width: 46,
          height: 46,
          borderRadius: 12,
          flexShrink: 0,
          background: "var(--primary-soft)",
          color: "var(--accent-strong)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Icon name={icon} size={24} />
      </span>
      <span>
        <span style={{ display: "block", fontWeight: 600, fontSize: 16 }}>{title}</span>
        <span className="muted" style={{ fontSize: 13 }}>{subtitle}</span>
      </span>
    </Link>
  );
}

export default function Home() {
  const { t } = useTranslation();
  const [fin, setFin] = useState(null);

  useEffect(() => {
    api.get("/finance/report/").then((r) => setFin(r.data)).catch(() => {});
  }, []);

  return (
    <>
      <h1>{t("home.title")}</h1>
      <p className="muted" style={{ marginTop: -6 }}>{t("home.subtitle")}</p>

      {fin && (
        <div className="stat-grid" style={{ margin: "16px 0 4px" }}>
          <div className="stat">
            <div className="label">{t("finance.revenue")}</div>
            <div className="value">{som(fin.revenue)}</div>
          </div>
          <div className="stat">
            <div className="label">{t("finance.profit")}</div>
            <div className="value" style={{ color: Number(fin.profit) >= 0 ? "var(--ok)" : "var(--danger)" }}>
              {som(fin.profit)}
            </div>
          </div>
          <div className="stat">
            <div className="label">{t("finance.clientDebt")}</div>
            <div className="value" style={{ color: Number(fin.client_debt) > 0 ? "var(--danger)" : "var(--ink)" }}>
              {som(fin.client_debt)}
            </div>
          </div>
        </div>
      )}

      <h3 style={{ marginTop: 22 }}>{t("home.whatToDo")}</h3>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12 }}>
        <Action to="/admin/finance" icon="clipboard" title={t("home.money")} subtitle={t("home.moneySub")} />
        <Action to="/admin/receipts" icon="receipt" title={t("home.receipts")} subtitle={t("home.receiptsSub")} />
        <Action to="/admin/catalog" icon="package" title={t("home.stock")} subtitle={t("home.stockSub")} />
        <Action to="/admin/pricing" icon="tag" title={t("home.prices")} subtitle={t("home.pricesSub")} />
      </div>

      <div
        className="card"
        style={{ marginTop: 16, background: "var(--canvas)", display: "flex", gap: 10, alignItems: "flex-start" }}
      >
        <Icon name="user" size={20} className="muted" />
        <p className="muted" style={{ margin: 0, fontSize: 14, lineHeight: 1.6 }}>{t("home.note")}</p>
      </div>

      <p style={{ marginTop: 16 }}>
        <Link to="/admin/dashboard" style={{ fontWeight: 600 }}>{t("home.analytics")} →</Link>
      </p>
    </>
  );
}
