import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import Icon from "../../components/Icon.jsx";
import ServiceFormModal from "../../components/ServiceFormModal.jsx";
import ServiceRecipeModal from "../../components/ServiceRecipeModal.jsx";
import { useUI } from "../../components/UIProvider.jsx";

// Which price fields drive each service kind's billing (mirrors the backend):
// area services (cutting / interior install) → master work rate per кв.м;
// exterior install → per piece; everything else → fixed base price.
function rateFields(service, t) {
  // Резка считается по погонному метру — у неё своё поле ставки, а не «за кв.м».
  // Ноль означает «своей ставки у станка нет», тогда берётся ставка материала.
  if (service.uses_running_meter) return [["rate_per_pm", t("pricing.ratePerPm")]];
  if (service.uses_area) return [["rate_flat", t("pricing.masterWork")]];
  if (service.uses_pieces) return [["rate_per_piece", t("pricing.ratePerPiece")]];
  return [["base_price", t("pricing.basePrice")]];
}

function ServiceCard({ service, materials, onSaved }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const fields = rateFields(service, t);
  // Название правится здесь же: опечатку в свежезаведённой услуге иначе
  // не исправить — только через Django-админку. Вид и станок не трогаем:
  // по ним считаются отчёты уже проданных строк.
  const [form, setForm] = useState({
    name: service.name,
    ...Object.fromEntries(fields.map(([key]) => [key, service[key]])),
  });
  const [busy, setBusy] = useState(false);
  const [recipes, setRecipes] = useState(false);

  async function save() {
    setBusy(true);
    try {
      await api.patch(`/services/services/${service.id}/`, form);
      onSaved?.();
      toast(t("common.saved"));
    } catch (e) {
      // Без этого стёртая ставка («сотру и впишу заново») уходила на сервер
      // пустой строкой, тот отвечал 400 «Требуется численное значение», а в
      // окне не появлялось НИЧЕГО: кнопка отжалась, цена осталась прежней.
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <input
          value={form.name}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
          style={{ fontWeight: 600, maxWidth: 360 }}
        />
        <div className="row" style={{ gap: 6, margin: 0 }}>
          {/* Станок — рядом с видом услуги: по нему группируется отчёт резки. */}
          {service.machine_display && <span className="badge">{service.machine_display}</span>}
          <span className="badge">{t(`serviceKind.${service.kind}`)}</span>
        </div>
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
            {key === "rate_per_pm" && (
              <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("pricing.ratePerPmHint")}</p>
            )}
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
        {/* Карта правится здесь же. Раньше строку расхода можно было завести
            только через Django-админку, и «клей списывается сам» оставалось
            обещанием: на складе он таял, а в системе стоял нетронутым. */}
        <button
          className="ghost"
          style={{ marginTop: 4, padding: 0, height: "auto", color: "var(--accent-strong)", fontWeight: 600 }}
          onClick={() => setRecipes(true)}
        >
          {t("recipes.edit")}
        </button>
      </div>

      {recipes && (
        <ServiceRecipeModal
          service={service}
          materials={materials}
          onClose={() => setRecipes(false)}
          onSaved={onSaved}
        />
      )}
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
  const { toast } = useUI();
  const [services, setServices] = useState([]);
  // Каталог нужен техкарте — выбрать расходник. page_size: без него приезжает
  // первая страница из 25, и клея в списке может не оказаться вовсе.
  const [materials, setMaterials] = useState([]);
  const [commission, setCommission] = useState("");
  const [savingC, setSavingC] = useState(false);
  const [creating, setCreating] = useState(false);

  function loadServices() {
    api.get("/services/services/").then((r) => setServices(r.data.results));
  }
  function loadMaterials() {
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results || []));
  }
  function loadSettings() {
    api.get("/services/settings/").then((r) => setCommission(r.data.master_commission_percent));
  }

  useEffect(() => {
    loadServices();
    loadSettings();
    loadMaterials();
  }, []);

  async function saveCommission() {
    setSavingC(true);
    try {
      await api.patch("/services/settings/", { master_commission_percent: commission });
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setSavingC(false);
    }
  }

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ margin: 0 }}>{t("pricing.title")}</h1>
        <button onClick={() => setCreating(true)}>
          <Icon name="plus" size={16} /> {t("pricing.newService")}
        </button>
      </div>
      <p className="muted" style={{ marginTop: 6 }}>{t("pricing.servicesOnlyHint")}</p>

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
          <ServiceCard key={s.id} service={s} materials={materials} onSaved={loadServices} />
        ))}

      {services.filter((s) => s.is_active !== false).length === 0 && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>{t("pricing.noServices")}</p>
        </div>
      )}

      {creating && <ServiceFormModal onClose={() => setCreating(false)} onSaved={loadServices} />}
    </>
  );
}
