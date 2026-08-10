import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import CatalogGrid from "../../components/CatalogGrid.jsx";
import DataTable from "../../components/DataTable.jsx";
import GalleryModal from "../../components/GalleryModal.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import ReceiveStockModal from "../../components/ReceiveStockModal.jsx";
import RefSelect from "../../components/RefSelect.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { apiError } from "../../api/errors.js";

const EMPTY = {
  name: "",
  type: "",
  thickness_mm: "",
  color: "",
  article: "",
  sheet_width: "",
  sheet_height: "",
  unit: "PIECE",
  is_roll_material: false,
  critical_balance: "0",
  purchase_price: "0",
  price_per_unit: "0",
};

const UNITS = ["SQM", "METER", "PIECE", "KG", "LITER"];

// Как назвался бы материал по заполненным полям. Подсказка, а не замена:
// у заказчика свои привычные подписи вроде «синий бишкек», отнимать их нельзя.
const trim = (v) => String(v).replace(/\.?0+$/, "").replace(".", ",");
// Число для показа: до сотых, без хвостовых нулей. В базе остаток лежит с
// четырьмя знаками — так целое количество листов не превращается в дробь.
const qty = (v) => Number(v || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
function suggestedName(m, types) {
  const type = types.find((x) => String(x.id) === String(m.type));
  const parts = [type?.name || "", m.color || ""];
  if (m.thickness_mm) parts.push(`${trim(m.thickness_mm)} мм`);
  if (m.article) parts.push(m.article);
  if (m.sheet_width && m.sheet_height) parts.push(`${trim(m.sheet_width)}×${trim(m.sheet_height)}`);
  return parts.filter(Boolean).join(" ").trim();
}

// Пустое числовое поле уходит из инпута строкой "" — сервер на неё отвечает
// «Требуется численное значение» и заворачивает ВЕСЬ материал. Ловится это
// легко: открыть материал, стереть цену, чтобы вписать заново, и сохранить.
//
// Толщина и размеры листа МОГУТ быть не заданы (у крепежа нет ни того, ни
// другого) — там пустое значение осмысленно и уходит как null.
const NULLABLE_NUMS = ["thickness_mm", "sheet_width", "sheet_height"];
// Цены и остатки null не принимают. Стёртая цена означает ноль — так её и
// отправляем, вместо того чтобы молча оставить прежнюю.
const ZERO_NUMS = [
  "critical_balance", "purchase_price", "price_per_unit", "price_per_sqm",
  "piece_price", "cut_rate_per_pm", "wholesale_price", "wholesale_min_qty",
];

function withNumbersFixed(material) {
  const out = { ...material };
  for (const key of NULLABLE_NUMS) {
    if (out[key] === "" || out[key] === undefined) out[key] = null;
  }
  for (const key of ZERO_NUMS) {
    if (out[key] === "") out[key] = 0;
  }
  return out;
}

// Module-level so inputs keep a stable identity (no focus loss on keystroke).
const NumField = ({ label, value, onChange, grow }) => (
  <div className={grow ? "field grow" : "field"} style={grow ? { margin: 0 } : undefined}>
    <label>{label}</label>
    <input type="number" step="any" value={value ?? ""} onChange={(e) => onChange(e.target.value)} />
  </div>
);
// Цена в таблице всегда с единицей. У материала с известной площадью листа под
// ценой за кв.м стоит цена листа — заказчик мыслит листами, и делить в уме,
// чтобы понять «а лист-то почём», он не должен.
const PriceCell = ({ m, value, t }) => {
  const per = m.is_roll_material || m.unit === "SQM" ? "кв.м" : t(`unit.${m.unit}`);
  const area = Number(m.piece_area) || 0;
  const num = Number(value) || 0;
  return (
    <>
      {num} <span className="muted">сом/{per}</span>
      {area > 0 && per === "кв.м" && num > 0 && (
        <div className="muted" style={{ fontSize: 12 }}>
          {Math.round(num * area)} {t("warehouse.perSheetShort")}
        </div>
      )}
    </>
  );
};
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
  const { toast, confirm } = useUI();
  const [materials, setMaterials] = useState([]);
  const [search, setSearch] = useState("");
  const [ordering, setOrdering] = useState("name");
  // Фильтры по разобранным полям вместо свободной категории: раньше тип,
  // толщина и цвет были зашиты в название, и отфильтровать было нечем.
  const [typeId, setTypeId] = useState("");
  const [color, setColor] = useState("");
  const [types, setTypes] = useState([]);
  const [sites, setSites] = useState([]);
  const [gallery, setGallery] = useState(null);
  const [editing, setEditing] = useState(null);
  const [receiving, setReceiving] = useState(null);
  const [bulk, setBulk] = useState(false);

  function load() {
    const params = { ordering };
    if (search) params.search = search;
    if (typeId) params.type = typeId;
    if (color) params.color = color;
    api.get("/warehouse/materials/", { params }).then((r) => setMaterials(r.data.results));
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, ordering, typeId, color]);

  // Справочники перечитываются и после того, как завели новое значение прямо
  // в форме материала или в сетке (RefSelect), иначе свежий тип не появился бы
  // в остальных выпадашках до перезагрузки страницы.
  function loadRefs() {
    return Promise.all([
      api.get("/warehouse/material-types/").then((r) => setTypes(r.data.results || r.data)),
      api.get("/warehouse/production-sites/").then((r) => setSites(r.data.results || r.data)),
    ]);
  }
  useEffect(() => {
    loadRefs();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const colors = [...new Set(materials.map((m) => m.color).filter(Boolean))];

  // Товар с историей продаж удалить нельзя — сервер прячет его из каталога,
  // чтобы суммы в старых чеках и отчётах не поехали задним числом.
  async function removeMaterial(m) {
    if (!(await confirm(t("warehouse.deleteConfirm", { name: m.name })))) return;
    try {
      const { data } = await api.delete(`/warehouse/materials/${m.id}/`);
      toast(data?.archived ? data.detail : t("warehouse.deleted"));
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function save() {
    try {
      const payload = withNumbersFixed(editing);
      if (editing.id) {
        await api.put(`/warehouse/materials/${editing.id}/`, payload);
      } else {
        await api.post("/warehouse/materials/", payload);
      }
      setEditing(null);
      toast(t("common.saved"));
      load();
    } catch (e) {
      // Без этого catch любая ошибка сервера пропадала молча: промис падал,
      // модалка оставалась открытой, сообщения не было — со стороны выглядело,
      // будто кнопка «Сохранить» просто не работает.
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const setF = (k) => (v) => setEditing({ ...editing, [k]: v });

  // --- Цена за лист и за кв.м -------------------------------------------
  //
  // Заказчик ПОКУПАЕТ листами и знает цену листа, а система считает в кв.м —
  // отсюда и вопрос «эта цифра за лист или за квадрат?». Теперь стоят оба поля:
  // заполняешь любое, второе считается по площади листа.
  const round2 = (n) => (Number.isFinite(n) ? String(Math.round(n * 100) / 100) : "");
  const sheetArea =
    Number(editing?.piece_area) ||
    Number(editing?.sheet_width) * Number(editing?.sheet_height) ||
    0;
  const toSheet = (perSqm) => (sheetArea ? round2(Number(perSqm || 0) * sheetArea) : "");
  const toSqm = (perSheet) => (sheetArea ? round2(Number(perSheet || 0) / sheetArea) : "");

  // Розничная цена — ДВА разных поля в базе (за кв.м и за лист), и цена листа
  // может быть назначена отдельно (скидка за целый лист). Поэтому пересчитываем
  // её только пока она «в паре» с ценой за кв.м: стоит вписать своё число — и
  // связь рвётся, ручное значение больше не затирается.
  function setSqmPrice(v) {
    const wasDerived =
      !Number(editing.piece_price) ||
      String(editing.piece_price) === String(toSheet(editing.price_per_sqm));
    setEditing({
      ...editing,
      price_per_sqm: v,
      ...(wasDerived && sheetArea ? { piece_price: toSheet(v) } : {}),
    });
  }

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
    {
      key: "type",
      label: t("warehouse.type"),
      render: (m) => (
        <span>
          {m.type_name && <span className="chip">{m.type_name}</span>}
          {m.thickness_mm != null && <span className="muted"> {trim(m.thickness_mm)} мм</span>}
          {m.color && <span className="muted">{m.thickness_mm != null ? " · " : " "}{m.color}</span>}
        </span>
      ),
    },
    {
      key: "quantity",
      label: t("common.quantity"),
      render: (m) => (
        <>
          {qty(m.quantity)} <span className="muted">{t(`unit.${m.unit}`)}</span>
          {m.sheets_remaining != null && (
            <span className="muted"> · ≈{Math.round(Number(m.sheets_remaining))} {t("warehouse.sheetsShort")}</span>
          )}
          {/* Пусто и «на исходе» — разные вещи. Только что заведённый каталог
              весь стоит на нуле, и красным он выглядит как авария, хотя просто
              ещё ничего не приходило. Красное — когда материал заканчивается,
              то есть остаток есть, но упал до порога; ноль — спокойный факт.
              Касса это уже различает, теперь и склад говорит так же. */}
          {Number(m.quantity) <= 0 ? (
            <span className="badge" style={{ marginLeft: 6 }}>
              {t("checkout.outOfStock")}
            </span>
          ) : (
            m.is_below_critical && (
              <span className="badge warn" style={{ marginLeft: 6 }}>
                {t("warehouse.lowStock")}
              </span>
            )
          )}
        </>
      ),
    },
    { key: "critical_balance", label: t("warehouse.critical") },
    // У закупочной цены единицы не было ВООБЩЕ: «980.00 сом» — за лист или за
    // квадрат? Рядом стояла розничная «1470 сом/кв.м», и две цифры выглядели
    // сравнимыми, хотя без единицы сравнивать их нельзя. Теперь единица есть у
    // обеих, а для листового материала под ценой за кв.м стоит цена листа.
    {
      key: "purchase_price",
      label: t("warehouse.purchasePrice"),
      render: (m) => <PriceCell m={m} value={m.purchase_price} t={t} />,
    },
    {
      key: "price_per_unit",
      label: t("warehouse.retailPrice"),
      render: (m) => (
        <PriceCell m={m} value={m.is_roll_material ? m.sqm_price : m.price_per_unit} t={t} />
      ),
    },
    {
      key: "actions",
      label: t("common.actions"),
      // «Изменить» и «Удалить» ПОДПИСАНЫ, а не спрятаны в серые иконки. Раньше
      // они стояли безымянными значками 17px под большой кнопкой «Поступление»
      // — заказчик их не нашёл и сообщил, что материал вообще нельзя ни
      // поправить, ни удалить. Функция была; не было видно, что она есть.
      render: (m) => (
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <button
            className="secondary row-btn"
            onClick={() => setReceiving(m)}
            title={t("supply.intake")}
          >
            <Icon name="inbox" size={14} /> {t("supply.intake")}
          </button>
          <button className="secondary row-btn" onClick={() => setEditing(m)}>
            <Icon name="pencil" size={14} /> {t("common.edit")}
          </button>
          <button className="ghost row-btn row-danger" onClick={() => removeMaterial(m)}>
            <Icon name="trash" size={14} /> {t("common.delete")}
          </button>
          <button className="ghost row-btn" onClick={() => setGallery(m)} title={t("warehouse.gallery")}>
            <Icon name="image" size={14} />
          </button>
        </div>
      ),
    },
  ];

  return (
    <>
      <div className="row" style={{ justifyContent: embedded ? "flex-end" : "space-between" }}>
        {!embedded && <h1>{t("warehouse.title")}</h1>}
        <div className="row" style={{ margin: 0, gap: 10 }}>
          {/* Пачкой — основной способ завести каталог: полсотни материалов
              модалкой по одной не заводят. */}
          <button className="secondary" onClick={() => setBulk(true)}>{t("grid.open")}</button>
          <button onClick={() => setEditing({ ...EMPTY })}>+ {t("warehouse.newMaterial")}</button>
        </div>
      </div>

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
            <option key={x.id} value={x.id}>{x.name}</option>
          ))}
        </select>
        {colors.length > 1 && (
          <select value={color} onChange={(e) => setColor(e.target.value)}>
            <option value="">{t("warehouse.allColors")}</option>
            {colors.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        )}
        <select value={ordering} onChange={(e) => setOrdering(e.target.value)}>
          <option value="name">{t("common.name")}</option>
          <option value="quantity">{t("common.quantity")}</option>
          <option value="price_per_unit">{t("warehouse.retailPrice")}</option>
          <option value="thickness_mm">{t("warehouse.thickness")}</option>
        </select>
      </div>

      <DataTable
        columns={columns}
        rows={materials}
        rowClass={(m) => (Number(m.quantity) > 0 && m.is_below_critical ? "warn" : "")}
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

      {bulk && (
        <Modal wide title={t("grid.title")} onClose={() => setBulk(false)}>
          <CatalogGrid
            types={types}
            sites={sites}
            onRefsChanged={loadRefs}
            onClose={() => setBulk(false)}
            onDone={() => {
              setBulk(false);
              load();
            }}
          />
        </Modal>
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
          {/* Свойства материала — отдельными полями. Раньше тип, толщина, цвет
              и размер писались внутрь названия, поэтому ни отфильтровать по
              толщине, ни вывести площадь листа из размера было нельзя. */}
          <div className="row">
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("warehouse.type")}</label>
              <RefSelect
                value={editing.type}
                options={types}
                endpoint="/warehouse/material-types/"
                onCreated={loadRefs}
                onChange={(v) => setEditing({ ...editing, type: v ? Number(v) : null })}
              />
            </div>
            <NumField grow label={t("warehouse.thickness")} value={editing.thickness_mm} onChange={setF("thickness_mm")} />
          </div>

          <div className="row">
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("warehouse.color")}</label>
              <input value={editing.color ?? ""} onChange={(e) => setEditing({ ...editing, color: e.target.value })} />
            </div>
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("warehouse.article")}</label>
              <input
                value={editing.article ?? ""}
                onChange={(e) => setEditing({ ...editing, article: e.target.value })}
                placeholder={t("warehouse.articlePh")}
              />
            </div>
          </div>

          <div className="row">
            <NumField grow label={t("warehouse.sheetWidth")} value={editing.sheet_width} onChange={setF("sheet_width")} />
            <NumField grow label={t("warehouse.sheetHeight")} value={editing.sheet_height} onChange={setF("sheet_height")} />
          </div>
          {editing.sheet_width && editing.sheet_height && (
            <p className="muted" style={{ fontSize: 12, margin: "-4px 0 0" }}>
              {t("warehouse.areaFromSize", {
                value: (Number(editing.sheet_width) * Number(editing.sheet_height)).toFixed(4),
              })}
            </p>
          )}

          <div className="field">
            <label>{t("common.name")}</label>
            <input value={editing.name ?? ""} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            {suggestedName(editing, types) && suggestedName(editing, types) !== editing.name && (
              <button
                className="ghost"
                style={{ marginTop: 4, color: "var(--accent-strong)", padding: 0, height: "auto" }}
                onClick={() => setEditing({ ...editing, name: suggestedName(editing, types) })}
              >
                {t("warehouse.useSuggested", { value: suggestedName(editing, types) })}
              </button>
            )}
          </div>

          <div className="row">
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

          {/* Колонка «производство» складской таблицы: откуда возят материал.
              Справочник, а не текст — опечатка иначе заводила бы ещё одно. */}
          <div className="field">
            <label>{t("warehouse.production")}</label>
            <RefSelect
              value={editing.production}
              options={sites}
              endpoint="/warehouse/production-sites/"
              onCreated={loadRefs}
              onChange={(v) => setEditing({ ...editing, production: v ? Number(v) : null })}
            />
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
              {/* Штучный материал: единица — штука, и это написано прямо в
                  подписи. Без неё «закупочная цена 30 сом» не отвечает на
                  вопрос «за что 30». */}
              <div className="row">
                <NumField
                  grow
                  label={`${t("warehouse.purchasePrice")}, ${t("warehouse.perUnitShort", { unit: t(`unit.${editing.unit || "PIECE"}`) })}`}
                  value={editing.purchase_price}
                  onChange={setF("purchase_price")}
                />
                <NumField
                  grow
                  label={`${t("warehouse.retailPrice")}, ${t("warehouse.perUnitShort", { unit: t(`unit.${editing.unit || "PIECE"}`) })}`}
                  value={editing.price_per_unit}
                  onChange={setF("price_per_unit")}
                />
              </div>
              <NumField label={t("warehouse.critical")} value={editing.critical_balance} onChange={setF("critical_balance")} />
            </>
          ) : (
            <>
              <p className="muted" style={{ fontSize: 12 }}>{t("warehouse.rollHint")}</p>

              <SectionLabel>{t("warehouse.priceStockSection")}</SectionLabel>
              {/* Закупка — одно значение в базе (за кв.м), но вводится любым из
                  двух полей: он покупает листами, а считается всё в квадратах. */}
              <div className="row">
                <NumField
                  grow
                  label={t("warehouse.purchasePerSqm")}
                  value={editing.purchase_price}
                  onChange={setF("purchase_price")}
                />
                {sheetArea > 0 && (
                  <NumField
                    grow
                    label={t("warehouse.purchasePerSheet")}
                    value={toSheet(editing.purchase_price)}
                    onChange={(v) => setF("purchase_price")(toSqm(v))}
                  />
                )}
              </div>
              <div className="row">
                <NumField grow label={t("warehouse.retailPerSqm")} value={editing.price_per_sqm} onChange={setSqmPrice} />
                <NumField grow label={t("pricing.cutRatePm")} value={editing.cut_rate_per_pm} onChange={setF("cut_rate_per_pm")} />
              </div>
              {sheetArea > 0 && (
                <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                  {t("warehouse.sheetAreaHint", { area: round2(sheetArea) })}
                </p>
              )}
              <NumField label={`${t("warehouse.critical")} (кв.м)`} value={editing.critical_balance} onChange={setF("critical_balance")} />

              <SectionLabel>{t("warehouse.sheetSale")}</SectionLabel>
              <div className="row">
                <NumField grow label={t("warehouse.retailPerSheet")} value={editing.piece_price} onChange={setF("piece_price")} />
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                {t("warehouse.piecePriceHint")}
              </p>
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
