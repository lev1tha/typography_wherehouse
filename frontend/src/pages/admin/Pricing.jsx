import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import Field, { focusFirstInvalid } from "../../components/Field.jsx";
import Icon from "../../components/Icon.jsx";
import ServiceFormModal from "../../components/ServiceFormModal.jsx";
import ServiceRecipeModal from "../../components/ServiceRecipeModal.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { formatNumber } from "../../utils/format.js";

// Which price fields drive each service kind's billing (mirrors the backend):
// area services (cutting / interior install) → master work rate per кв.м;
// exterior install → per piece; everything else → fixed base price.
function rateFields(service, t) {
  // Резка считается по погонному метру — у неё своё поле ставки, а не «за кв.м».
  // Ноль означает «своей ставки у станка нет», тогда берётся ставка материала.
  if (service.uses_running_meter) return [["rate_per_pm", t("pricing.ratePerPm")]];
  // Гравировка — цена за кв.м. Это базовая цена: в кассе её меняют по заказу.
  if (service.kind === "ENGRAVING") return [["rate_flat", t("pricing.engravingRate")]];
  // Отходы — по цене на КАЖДУЮ мерку: отходы листа меряют квадратами, рулона —
  // метрами, штучного — штуками, и прайс на них разный.
  if (service.uses_free_measure)
    return [
      ["rate_flat", t("pricing.wasteRateSqm")],
      ["rate_per_pm", t("pricing.wasteRatePm")],
      ["rate_per_piece", t("pricing.wasteRatePiece")],
    ];
  if (service.uses_area) return [["rate_flat", t("pricing.masterWork")]];
  if (service.uses_pieces) return [["rate_per_piece", t("pricing.ratePerPiece")]];
  return [["base_price", t("pricing.basePrice")]];
}

// Цена приходит с сервера как «3000.00»: в поле ввода хвост нулей — шум, и он
// отличается от того, как та же сумма выглядит на остальных экранах.
const clean = (v) => (v == null || v === "" || Number.isNaN(Number(v)) ? "" : String(+Number(v)));

function ServiceCard({ service, materials, onSaved }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const fields = rateFields(service, t);
  // Название правится здесь же: опечатку в свежезаведённой услуге иначе
  // не исправить — только через Django-админку. Вид и станок не трогаем:
  // по ним считаются отчёты уже проданных строк.
  const [form, setForm] = useState({
    name: service.name,
    ...Object.fromEntries(fields.map(([key]) => [key, clean(service[key])])),
    // Минимум строки этой услуги: пусто — действует общий из «Правил прайса».
    min_line_amount: clean(service.min_line_amount),
  });
  const [busy, setBusy] = useState(false);
  const [recipes, setRecipes] = useState(false);
  const [errors, setErrors] = useState({});

  async function save() {
    // Стёртая ставка («сотру и впишу заново») ушла бы на сервер пустой строкой
    // и вернулась бы 400 — называем поле сами, рядом с ним.
    const bad = {};
    fields.forEach(([key]) => {
      if (form[key] === "" || Number.isNaN(Number(form[key]))) bad[key] = t("common.needValue");
    });
    if (!form.name.trim()) bad.name = t("common.needValue");
    if (form.min_line_amount !== "" && !(Number(form.min_line_amount) >= 0)) bad.min_line_amount = t("pricing.minLineBad");
    setErrors(bad);
    if (Object.keys(bad).length) return focusFirstInvalid();
    setBusy(true);
    try {
      await api.patch(`/services/services/${service.id}/`, {
        ...form,
        min_line_amount: form.min_line_amount === "" ? null : form.min_line_amount,
      });
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
        <Field error={errors.name} style={{ margin: 0 }}>
          <input
            aria-label={t("pricing.serviceName")}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            style={{ fontWeight: 600, maxWidth: 360 }}
          />
        </Field>
        <div className="row" style={{ gap: 6, margin: 0 }}>
          {/* Станок — рядом с видом услуги: по нему группируется отчёт резки. */}
          {service.machine_display && <span className="badge">{service.machine_display}</span>}
          <span className="badge">{t(`serviceKind.${service.kind}`)}</span>
        </div>
      </div>
      <div className="row" style={{ marginTop: 10 }}>
        {fields.map(([key, label]) => (
          <Field
            className="grow"
            key={key}
            label={label}
            required
            error={errors[key]}
            hint={
              key === "rate_per_pm" && service.uses_running_meter
                ? t("pricing.ratePerPmHint")
                : service.kind === "ENGRAVING"
                ? t("pricing.engravingRateHint")
                : undefined
            }
          >
            <input
              type="number"
              inputMode="decimal"
              value={form[key]}
              onChange={(e) => setForm({ ...form, [key]: e.target.value })}
            />
          </Field>
        ))}
        <Field
          className="grow"
          label={t("pricing.minLineService")}
          error={errors.min_line_amount}
          hint={t("pricing.minLineServiceHint")}
        >
          <input
            type="number"
            inputMode="decimal"
            min="0"
            placeholder={t("pricing.minLineServicePh")}
            value={form.min_line_amount}
            onChange={(e) => setForm({ ...form, min_line_amount: e.target.value })}
          />
        </Field>
      </div>
      {/* Подсказка про отходы — одна на карточку: полей у них три, и под
          каждым она повторялась бы трижды. */}
      {service.uses_free_measure && (
        <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>{t("pricing.wasteRateHint")}</p>
      )}
      <div className="field">
        <label>{t("pricing.recipes")}</label>
        {service.recipes?.length ? (
          service.recipes.map((r) => (
            <div className="recipe-row" key={r.id}>
              <span>{r.material_name}</span>
              <span className="muted recipe-qty">
                {formatNumber(r.consumption_per_unit, { max: 3 })}{" "}
                / {r.consumption_mode === "PER_SQM" ? t("pricing.perSqm") : t("pricing.perOrder")}
              </span>
            </div>
          ))
        ) : (
          <div className="muted">{t("common.empty")}</div>
        )}
        {/* Карта правится здесь же. Раньше строку расхода можно было завести
            только через Django-админку, и «клей списывается сам» оставалось
            обещанием: на складе он таял, а в системе стоял нетронутым. */}
        <button
          className="ghost recipe-edit"
          style={{ padding: 0, height: "auto", color: "var(--accent-ink)", fontWeight: 600 }}
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
  // Правила прайса (CALC-01): общий минимум строки услуги и наценка за
  // срочность. 0 — правило выключено, цены как раньше.
  const [rules, setRules] = useState({ min_line_amount: "", urgency_percent: "" });
  const [rulesErr, setRulesErr] = useState({});
  const [savingR, setSavingR] = useState(false);
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
    api.get("/services/settings/").then((r) => {
      setCommission(r.data.master_commission_percent);
      setRules({
        min_line_amount: clean(r.data.min_line_amount),
        urgency_percent: clean(r.data.urgency_percent),
      });
    });
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

  async function saveRules() {
    const bad = {};
    if (rules.min_line_amount === "" || !(Number(rules.min_line_amount) >= 0)) bad.min_line_amount = t("common.needValue");
    if (rules.urgency_percent === "" || !(Number(rules.urgency_percent) >= 0)) bad.urgency_percent = t("common.needValue");
    setRulesErr(bad);
    if (Object.keys(bad).length) return focusFirstInvalid();
    setSavingR(true);
    try {
      await api.patch("/services/settings/", rules);
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setSavingR(false);
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
          <Field className="grow" style={{ margin: 0 }} label={t("pricing.masterCommission")}>
            <input type="number" inputMode="decimal" value={commission} onChange={(e) => setCommission(e.target.value)} />
          </Field>
          <button onClick={saveCommission} disabled={savingC}>{t("common.save")}</button>
        </div>
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{t("pricing.masterCommissionHint")}</p>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3 style={{ marginTop: 0 }}>{t("pricing.rulesTitle")}</h3>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <Field
            className="grow"
            label={t("pricing.minLine")}
            error={rulesErr.min_line_amount}
            hint={t("pricing.minLineHint")}
          >
            <input
              type="number"
              inputMode="decimal"
              min="0"
              value={rules.min_line_amount}
              onChange={(e) => setRules({ ...rules, min_line_amount: e.target.value })}
            />
          </Field>
          <Field
            className="grow"
            label={t("pricing.urgency")}
            error={rulesErr.urgency_percent}
            hint={t("pricing.urgencyHint")}
          >
            <input
              type="number"
              inputMode="decimal"
              min="0"
              value={rules.urgency_percent}
              onChange={(e) => setRules({ ...rules, urgency_percent: e.target.value })}
            />
          </Field>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>{t("pricing.rulesOrder")}</p>
        <button onClick={saveRules} disabled={savingR}>{t("common.save")}</button>
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
