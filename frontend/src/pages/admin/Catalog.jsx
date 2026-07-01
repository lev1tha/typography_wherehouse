import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DataTable from "../../components/DataTable.jsx";
import GalleryModal from "../../components/GalleryModal.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import ReceiveStockModal from "../../components/ReceiveStockModal.jsx";

const EMPTY = {
  name: "",
  category: "",
  unit: "PIECE",
  is_roll_material: false,
  critical_balance: "0",
  purchase_price: "0",
  price_per_unit: "0",
};

const UNITS = ["SQM", "METER", "PIECE", "KG", "LITER"];

// Module-level so inputs keep a stable identity (no focus loss on keystroke).
const NumField = ({ label, value, onChange, grow }) => (
  <div className={grow ? "field grow" : "field"} style={grow ? { margin: 0 } : undefined}>
    <label>{label}</label>
    <input type="number" step="any" value={value ?? ""} onChange={(e) => onChange(e.target.value)} />
  </div>
);
const SectionLabel = ({ children }) => (
  <div
    style={{
      fontWeight: 600,
      fontSize: 13,
      color: "var(--ink-secondary)",
      margin: "18px 0 8px",
      paddingTop: 12,
      borderTop: "1px solid var(--hairline)",
    }}
  >
    {children}
  </div>
);

export default function Catalog({ embedded = false }) {
  const { t } = useTranslation();
  const [materials, setMaterials] = useState([]);
  const [search, setSearch] = useState("");
  const [ordering, setOrdering] = useState("name");
  const [category, setCategory] = useState("");
  const [gallery, setGallery] = useState(null);
  const [editing, setEditing] = useState(null);
  const [receiving, setReceiving] = useState(null);

  function load() {
    const params = { ordering };
    if (search) params.search = search;
    if (category) params.category = category;
    api.get("/warehouse/materials/", { params }).then((r) => setMaterials(r.data.results));
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, ordering, category]);

  const categories = [...new Set(materials.map((m) => m.category))];

  async function save() {
    const payload = { ...editing };
    if (editing.id) {
      await api.put(`/warehouse/materials/${editing.id}/`, payload);
    } else {
      await api.post("/warehouse/materials/", payload);
    }
    setEditing(null);
    load();
  }

  const setF = (k) => (v) => setEditing({ ...editing, [k]: v });

  const columns = [
    {
      key: "img",
      label: "",
      render: (m) =>
        m.primary_image ? (
          <img className="thumb" src={m.primary_image} alt="" onClick={() => setGallery(m)} style={{ cursor: "pointer" }} />
        ) : (
          <div className="thumb" style={{ display: "flex", alignItems: "center", justifyContent: "center", color: "var(--ink-faint)" }}><Icon name="image" size={22} /></div>
        ),
    },
    {
      key: "name",
      label: t("common.name"),
      render: (m) => (
        <>
          <strong>{m.name}</strong>
          {m.is_roll_material && <span className="chip" style={{ marginLeft: 6 }}>{t(`unit.${m.unit}`)}</span>}
        </>
      ),
    },
    { key: "category", label: t("common.category"), render: (m) => <span className="chip">{m.category}</span> },
    {
      key: "quantity",
      label: t("common.quantity"),
      render: (m) => (
        <>
          {m.quantity} <span className="muted">{t(`unit.${m.unit}`)}</span>
          {m.sheets_remaining != null && (
            <span className="muted"> · ≈{Math.round(Number(m.sheets_remaining))} {t("warehouse.sheetsShort")}</span>
          )}
          {m.is_below_critical && (
            <span className="badge warn" style={{ marginLeft: 6 }}>
              {t("warehouse.lowStock")}
            </span>
          )}
        </>
      ),
    },
    { key: "critical_balance", label: t("warehouse.critical") },
    { key: "purchase_price", label: t("warehouse.purchasePrice"), render: (m) => `${m.purchase_price} сом` },
    {
      key: "price_per_unit",
      label: t("warehouse.retailPrice"),
      render: (m) =>
        m.is_roll_material ? `${m.sqm_price} сом/кв.м` : `${m.price_per_unit} сом`,
    },
    {
      key: "actions",
      label: t("common.actions"),
      render: (m) => (
        <div className="row" style={{ gap: 6 }}>
          <button
            className="secondary"
            style={{ padding: "5px 10px", height: "auto", display: "inline-flex", alignItems: "center", gap: 5, whiteSpace: "nowrap" }}
            onClick={() => setReceiving(m)}
            title={t("supply.intake")}
          >
            <Icon name="inbox" size={16} /> {t("supply.intake")}
          </button>
          <button className="ghost" onClick={() => setGallery(m)} aria-label={t("warehouse.gallery")}>
            <Icon name="image" size={18} />
          </button>
          <button className="ghost" onClick={() => setEditing(m)} aria-label={t("common.edit")}>
            <Icon name="pencil" size={17} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <>
      <div className="row" style={{ justifyContent: embedded ? "flex-end" : "space-between" }}>
        {!embedded && <h1>{t("warehouse.title")}</h1>}
        <button onClick={() => setEditing({ ...EMPTY })}>+ {t("warehouse.newMaterial")}</button>
      </div>

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
        <select value={ordering} onChange={(e) => setOrdering(e.target.value)}>
          <option value="name">{t("common.name")}</option>
          <option value="quantity">{t("common.quantity")}</option>
          <option value="price_per_unit">{t("warehouse.retailPrice")}</option>
          <option value="category">{t("common.category")}</option>
        </select>
      </div>

      <DataTable
        columns={columns}
        rows={materials}
        rowClass={(m) => (m.is_below_critical ? "warn" : "")}
      />

      {gallery && (
        <GalleryModal
          material={gallery}
          manage
          onClose={() => setGallery(null)}
          onChanged={load}
        />
      )}

      {receiving && (
        <ReceiveStockModal
          material={receiving}
          onClose={() => setReceiving(null)}
          onDone={load}
        />
      )}

      {editing && (
        <Modal
          title={editing.id ? editing.name : t("warehouse.newMaterial")}
          onClose={() => setEditing(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setEditing(null)}>
                {t("common.cancel")}
              </button>
              <button onClick={save}>{t("common.save")}</button>
            </>
          }
        >
          <div className="field">
            <label>{t("common.name")}</label>
            <input value={editing.name ?? ""} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
          </div>

          <div className="row">
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("common.category")}</label>
              <input value={editing.category ?? ""} onChange={(e) => setEditing({ ...editing, category: e.target.value })} />
            </div>
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("warehouse.unit")}</label>
              <select
                value={editing.unit ?? "PIECE"}
                disabled={!!editing.is_roll_material}
                onChange={(e) => setEditing({ ...editing, unit: e.target.value })}
              >
                {UNITS.map((u) => (
                  <option key={u} value={u}>{t(`unit.${u}`)}</option>
                ))}
              </select>
            </div>
          </div>

          <label className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              style={{ width: 20, height: 20, minHeight: 0 }}
              checked={!!editing.is_roll_material}
              onChange={(e) =>
                setEditing({
                  ...editing,
                  is_roll_material: e.target.checked,
                  unit: e.target.checked ? "SQM" : editing.unit,
                })
              }
            />
            {t("warehouse.isRoll")}
          </label>

          {!editing.is_roll_material ? (
            <>
              <SectionLabel>{t("warehouse.priceStockSection")}</SectionLabel>
              <div className="row">
                <NumField grow label={t("warehouse.purchasePrice")} value={editing.purchase_price} onChange={setF("purchase_price")} />
                <NumField grow label={t("warehouse.retailPrice")} value={editing.price_per_unit} onChange={setF("price_per_unit")} />
              </div>
              <NumField label={t("warehouse.critical")} value={editing.critical_balance} onChange={setF("critical_balance")} />
            </>
          ) : (
            <>
              <p className="muted" style={{ fontSize: 12 }}>{t("warehouse.rollHint")}</p>

              <SectionLabel>{t("warehouse.priceStockSection")}</SectionLabel>
              <div className="row">
                <NumField grow label={t("pricing.pricePerSqm")} value={editing.price_per_sqm} onChange={setF("price_per_sqm")} />
                <NumField grow label={t("pricing.cutRatePm")} value={editing.cut_rate_per_pm} onChange={setF("cut_rate_per_pm")} />
              </div>
              <NumField label={`${t("warehouse.critical")} (кв.м)`} value={editing.critical_balance} onChange={setF("critical_balance")} />

              <SectionLabel>{t("warehouse.sheetSale")}</SectionLabel>
              <div className="row">
                <NumField grow label={t("warehouse.pieceArea")} value={editing.piece_area} onChange={setF("piece_area")} />
                <NumField grow label={t("warehouse.piecePrice")} value={editing.piece_price} onChange={setF("piece_price")} />
              </div>
              <div className="row">
                <NumField grow label={t("warehouse.wholesalePrice")} value={editing.wholesale_price} onChange={setF("wholesale_price")} />
                <NumField grow label={t("warehouse.wholesaleMin")} value={editing.wholesale_min_qty} onChange={setF("wholesale_min_qty")} />
              </div>
              <p className="muted" style={{ fontSize: 12 }}>{t("warehouse.wholesaleHint")}</p>
            </>
          )}

          {editing.id != null && (
            <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>
              {t("common.quantity")}: {editing.quantity ?? 0} — {t("warehouse.qtyNote")}
            </p>
          )}
        </Modal>
      )}
    </>
  );
}
