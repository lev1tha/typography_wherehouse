import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import GalleryModal from "../../components/GalleryModal.jsx";
import Icon from "../../components/Icon.jsx";

export default function Warehouse() {
  const { t } = useTranslation();
  const [materials, setMaterials] = useState([]);
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [gallery, setGallery] = useState(null);

  function load() {
    const params = { ordering: "name" };
    if (search) params.search = search;
    if (category) params.category = category;
    api.get("/warehouse/materials/", { params }).then((r) => setMaterials(r.data.results));
  }
  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, category]);

  const categories = [...new Set(materials.map((m) => m.category))];

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
        <select value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">{t("common.all")}</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
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
              background: m.is_below_critical ? "var(--warn-bg)" : "var(--surface)",
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
                {m.is_below_critical && <span className="badge warn">{t("warehouse.lowStock")}</span>}
              </div>
              <div className="muted">{m.category}</div>
              <div className="crow">
                <span className="k">{t("common.quantity")}</span>
                <span>
                  {m.sheets_remaining != null
                    ? `${m.quantity} кв.м · ≈${Math.round(Number(m.sheets_remaining))} ${t("warehouse.sheetsShort")}`
                    : m.quantity}
                </span>
              </div>
              <div className="crow">
                <span className="k">{t("warehouse.retailPrice")}</span>
                <span>{m.price_per_unit} сом</span>
              </div>
            </div>
          </div>
        ))}
      </div>

      {gallery && <GalleryModal material={gallery} onClose={() => setGallery(null)} />}
    </>
  );
}
