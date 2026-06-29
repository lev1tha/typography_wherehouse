import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";

// Which price fields drive each service kind's billing (mirrors the backend):
// area services (cutting / interior install) → master work rate per кв.м;
// exterior install → per piece; everything else → fixed base price.
function rateFields(service, t) {
  if (service.uses_area) return [["rate_flat", t("pricing.masterWork")]];
  if (service.uses_pieces) return [["rate_per_piece", t("pricing.ratePerPiece")]];
  return [["base_price", t("pricing.basePrice")]];
}

function ServiceCard({ service, onSaved }) {
  const { t } = useTranslation();
  const fields = rateFields(service, t);
  const [form, setForm] = useState(Object.fromEntries(fields.map(([key]) => [key, service[key]])));
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      await api.patch(`/services/services/${service.id}/`, form);
      onSaved?.();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>{service.name}</h3>
        <span className="badge">{t(`serviceKind.${service.kind}`)}</span>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        {fields.map(([key, label]) => (
          <div className="field grow" key={key}>
            <label>{label}</label>
            <input
              type="number"
              value={form[key]}
              onChange={(e) => setForm({ ...form, [key]: e.target.value })}
            />
          </div>
        ))}
      </div>
      <div className="field">
        <label>{t("pricing.recipes")}</label>
        {service.recipes?.length ? (
          service.recipes.map((r) => (
            <div className="crow" key={r.id}>
              <span>{r.material_name}</span>
              <span className="muted">
                {r.consumption_per_unit} {r.consumption_mode === "PER_SQM" ? "/ кв.м" : "/ заказ"}
              </span>
            </div>
          ))
        ) : (
          <span className="muted">{t("common.empty")}</span>
        )}
      </div>
      <button onClick={save} disabled={busy}>
        {t("common.save")}
      </button>
    </div>
  );
}

// Эта страница — ТОЛЬКО про работу/услуги (ставки + % мастеру). Цены
// материалов живут в разделе «Склад» (карточка материала), чтобы не было
// дублирования: материал и его цена редактируются в одном месте.
export default function Pricing() {
  const { t } = useTranslation();
  const [services, setServices] = useState([]);
  const [commission, setCommission] = useState("");
  const [savingC, setSavingC] = useState(false);

  function loadServices() {
    api.get("/services/services/").then((r) => setServices(r.data.results));
  }
  function loadSettings() {
    api.get("/services/settings/").then((r) => setCommission(r.data.master_commission_percent));
  }

  useEffect(() => {
    loadServices();
    loadSettings();
  }, []);

  async function saveCommission() {
    setSavingC(true);
    try {
      await api.patch("/services/settings/", { master_commission_percent: commission });
    } finally {
      setSavingC(false);
    }
  }

  return (
    <>
      <h1>{t("pricing.title")}</h1>
      <p className="muted" style={{ marginTop: -6 }}>{t("pricing.servicesOnlyHint")}</p>

      {/* Master wage % — admin only, hidden from cashiers */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end" }}>
          <div className="field grow" style={{ margin: 0 }}>
            <label>{t("pricing.masterCommission")}</label>
            <input type="number" value={commission} onChange={(e) => setCommission(e.target.value)} />
          </div>
          <button onClick={saveCommission} disabled={savingC}>{t("common.save")}</button>
        </div>
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{t("pricing.masterCommissionHint")}</p>
      </div>

      {services
        .filter((s) => s.is_active !== false)
        .map((s) => (
          <ServiceCard key={s.id} service={s} onSaved={loadServices} />
        ))}
    </>
  );
}
