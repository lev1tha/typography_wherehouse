import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import GalleryModal from "../../components/GalleryModal.jsx";
import Icon from "../../components/Icon.jsx";

// Остаток до сотых, без хвостовых нулей — как в каталоге админа. В базе он
// хранится с четырьмя знаками, чтобы целые листы не превращались в дробь.
const qty = (v) => Number(v || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
// Толщина без хвостовых нулей и с запятой — «2,5 мм», как её пишет заказчик.
const trim = (v) => String(v).replace(/\.?0+$/, "").replace(".", ",");

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

export default function Warehouse() {
  const { t } = useTranslation();
  const [materials, setMaterials] = useState([]);
  const [search, setSearch] = useState("");
  // Фильтр по ТИПУ материала. Раньше он строился по `category` — полю, которого
  // после разбора номенклатуры не существует: список был пустым, а параметр
  // запроса сервер игнорировал, то есть фильтр не работал вообще.
  const [typeId, setTypeId] = useState("");
  const [types, setTypes] = useState([]);
  const [gallery, setGallery] = useState(null);

  function load() {
    const params = { ordering: "name", page_size: 500 };
    if (search) params.search = search;
    if (typeId) params.type = typeId;
    api.get("/warehouse/materials/", { params }).then((r) => setMaterials(r.data.results));
  }
  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, typeId]);

  useEffect(() => {
    api.get("/warehouse/material-types/").then((r) => setTypes(r.data.results || r.data));
  }, []);

  return (
    <>
      <h1>{t("warehouse.title")}</h1>
      <div className="toolbar">
        <input
          className="search"
          placeholder={t("common.search")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={typeId} onChange={(e) => setTypeId(e.target.value)}>
          <option value="">{t("warehouse.allTypes")}</option>
          {types.map((x) => (
            <option key={x.id} value={x.id}>
              {x.name}
            </option>
          ))}
        </select>
      </div>

      <div className="stat-grid">
        {materials.map((m) => (
          <div
            key={m.id}
            className="card"
            style={{
              padding: 0,
              overflow: "hidden",
              // Красным — только то, что заканчивается. Нулевой остаток у ещё
              // не закупленного материала не авария (см. Catalog.jsx).
              background:
                Number(m.quantity) > 0 && m.is_below_critical
                  ? "var(--warn-bg)"
                  : "var(--surface)",
            }}
          >
            {m.primary_image ? (
              <div
                onClick={() => setGallery(m)}
                style={{ height: 150, background: "var(--canvas)", cursor: "pointer" }}
              >
                <img
                  src={m.primary_image}
                  alt={m.name}
                  style={{ width: "100%", height: "100%", objectFit: "cover" }}
                />
              </div>
            ) : (
              <div
                style={{
                  height: 44,
                  background: "var(--canvas)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "var(--ink-faint)",
                }}
                title={t("common.empty")}
              >
                <Icon name="image" size={20} />
              </div>
            )}
            <div style={{ padding: 14 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <strong>{m.name}</strong>
                {Number(m.quantity) <= 0 ? (
                  <span className="badge">{t("checkout.outOfStock")}</span>
                ) : (
                  m.is_below_critical && (
                    <span className="badge warn">{t("warehouse.lowStock")}</span>
                  )
                )}
              </div>
              {/* Свойства материала: раньше здесь выводилось `m.category` —
                  поле, удалённое при разборе номенклатуры на поля, то есть
                  пустая строка. Показываем то же, что в каталоге админа. */}
              <div className="muted">
                {[m.type_name, m.thickness_mm != null ? `${trim(m.thickness_mm)} ${t("unit.MM")}` : "", m.color]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
              <div className="crow">
                <span className="k">{t("common.quantity")}</span>
                <span>
                  {/* Остаток лежит с четырьмя знаками (ради целых листов), но
                      показывать «2.9768 кв.м» незачем — округляем, как в каталоге. */}
                  {qty(m.quantity)} {t(`unit.${m.unit}`)}
                  {m.sheets_remaining != null && (
                    <> · ≈{Math.round(Number(m.sheets_remaining))} {t("warehouse.sheetsShort")}</>
                  )}
                </span>
              </div>
              <div className="crow">
                <span className="k">{t("warehouse.retailPrice")}</span>
                {/* У листового материала цена лежит в цене за кв.м, а
                    price_per_unit равен нулю — склад показывал «0.00 сом»
                    на всей номенклатуре. */}
                {/* Суммы — как везде в системе: «1 500 сом», а не сырые
                    «1500.00» из API. Один и тот же материал показывался
                    складовщику с копейками, а в кассе и каталоге — без. */}
                <span>
                  {m.is_roll_material
                    ? `${som(m.sqm_price)} ${t("warehouse.perUnitShort", { unit: t("unit.SQM") })}`
                    : som(m.price_per_unit)}
                </span>
              </div>
            </div>
          </div>
        ))}
      </div>

      {gallery && <GalleryModal material={gallery} onClose={() => setGallery(null)} />}
    </>
  );
}
