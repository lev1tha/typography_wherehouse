import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import AdjustStockModal from "../../components/AdjustStockModal.jsx";
import RollStocktakeModal from "../../components/RollStocktakeModal.jsx";
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
  intake_form: "SHEET",
  critical_balance: "0",
  purchase_price: "0",
  roll_width: "",
  price_per_pm: "0",
  price_per_unit: "0",
};

const PIECE_UNITS = ["PIECE", "KG", "LITER"];

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
  "roll_width", "price_per_pm",
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
//
// Пока в поле ПЕЧАТАЮТ, показываем ровно набранное (`draft`), а не то, что
// вернулось из состояния. Без этого поле, значение которого считается из
// соседнего — закупка за лист ↔ за кв.м, — подменяло первую же цифру
// результатом округления: набираешь 3400, поле делит на площадь листа
// (3 ÷ 2,9768 = 1,01), умножает обратно (3,01) и ставит «3,01» вместо «3».
// Следующая цифра дописывалась уже к нему, и цену нельзя было выставить
// вообще — ни закупочную, ни за лист, ни у одного материала.
// На выходе из поля показываем сохранённое значение: оно может отличаться на
// копейку, потому что за кв.м в базе лежит с двумя знаками.
// `disabled` + `placeholder` — чтобы поле, которое пока нечем заполнить, стояло
// на месте серым и с объяснением, а не исчезало. Исчезнувшее поле читается как
// «такого в системе нет», и заказчик приходит спрашивать, где оно.
const NumField = ({ label, value, onChange, grow, disabled, placeholder }) => {
  const [draft, setDraft] = useState(null);
  return (
    <div className={grow ? "field grow" : "field"} style={grow ? { margin: 0 } : undefined}>
      <label>{label}</label>
      <input
        type="number"
        step="any"
        disabled={disabled}
        placeholder={placeholder}
        value={draft ?? value ?? ""}
        onChange={(e) => {
          setDraft(e.target.value);
          onChange(e.target.value);
        }}
        onBlur={() => setDraft(null)}
      />
    </div>
  );
};
// Цена в таблице всегда с единицей. У материала с известной площадью листа под
// ценой за кв.м стоит цена листа — заказчик мыслит листами, и делить в уме,
// чтобы понять «а лист-то почём», он не должен.
//
// У РОЗНИЧНОЙ цены строка листа — это `piece_price`, по которой лист и продают
// в кассе, с тем же округлением вверх до сома, что и в кассе. Раньше здесь
// стояло `цена за кв.м × площадь`, и склад показывал 3725 сом/лист там, где
// касса продавала за 3700 (или 4465 против 4466 из-за разного округления) —
// две цифры одной цены на соседних экранах. Нет цены за лист — нет и строки:
// продажа целиком в кассе тогда недоступна, показывать выдуманную цифру нечего.
// У ЗАКУПОЧНОЙ цены своей «за лист» в базе нет — она по-прежнему считается из
// цены за кв.м.
const ceilSom = (v) => Math.max(0, Math.ceil((Number(v) || 0) - 1e-6));
const PriceCell = ({ m, value, t, pieceValue }) => {
  // Единицу берём из словаря, а не строкой: в английском интерфейсе рядом
  // стояло «20,84 кв.м · ≈7 sheets» — половина строки на чужом языке.
  const bySqm = m.is_roll_material || m.unit === "SQM";
  const per = bySqm ? t("unit.SQM") : t(`unit.${m.unit}`);
  const area = Number(m.piece_area) || 0;
  const num = Number(value) || 0;
  const explicitPiece = pieceValue !== undefined;
  const sheet = explicitPiece ? ceilSom(pieceValue) : Math.round(num * area);
  const showSheet = bySqm && (explicitPiece ? sheet > 0 : area > 0 && num > 0);
  return (
    <>
      {explicitPiece ? ceilSom(num) : num}{" "}
      <span className="muted">{t("warehouse.perUnitShort", { unit: per })}</span>
      {showSheet && (
        <div className="muted" style={{ fontSize: 12 }}>
          {sheet} {t("warehouse.perSheetShort")}
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
  // Форма материала (штучный / лист / рулон) — свой фильтр: у листа и рулона
  // разная приёмка и разная продажа, и «покажи только рулоны» это первое, что
  // спрашивают, когда номенклатура перевалила за полсотни строк.
  const [form, setForm] = useState("");
  const [types, setTypes] = useState([]);
  const [sites, setSites] = useState([]);
  const [gallery, setGallery] = useState(null);
  const [editing, setEditing] = useState(null);
  const [receiving, setReceiving] = useState(null);
  const [adjusting, setAdjusting] = useState(null);
  const [bulk, setBulk] = useState(false);

  function load() {
    // page_size: без него приезжает первая страница из 25 материалов, и
    // каталог из полусотни позиций обрывался на 25-й — молча, без всякой
    // пагинации на экране: материала просто не было в списке. Во всех
    // остальных списках материалов (касса, приход, обзор) он уже стоял.
    const params = { ordering, page_size: 500 };
    if (search) params.search = search;
    if (typeId) params.type = typeId;
    if (color) params.color = color;
    if (form) params.form = form;
    api.get("/warehouse/materials/", { params }).then((r) => setMaterials(r.data.results));
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, ordering, typeId, color, form]);

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
      // Сервер сам говорит, что произошло: удалён насовсем (вместе с приходами)
      // или спрятан, потому что по нему были продажи.
      toast(data?.detail || t("warehouse.deleted"));
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
  // отсюда и вопрос «эта цифра за лист или за квадрат?». Поэтому стоят оба поля.
  // У ЗАКУПКИ это одно число в базе, показанное двумя способами, — там пересчёт
  // в обе стороны и всегда. У РОЗНИЦЫ это две независимые цены, и пересчёт
  // работает один раз, при заполнении пустого поля (см. setSqmPrice ниже).
  const round2 = (n) => (Number.isFinite(n) ? String(Math.round(n * 100) / 100) : "");
  const sheetArea =
    Number(editing?.piece_area) ||
    Number(editing?.sheet_width) * Number(editing?.sheet_height) ||
    0;
  const toSheet = (perSqm) => (sheetArea ? round2(Number(perSqm || 0) * sheetArea) : "");
  const toSqm = (perSheet) => (sheetArea ? round2(Number(perSheet || 0) / sheetArea) : "");
  // Показываем ЦЕЛОЕ число сомов, если оно означает ровно ту же цену за кв.м.
  //
  // В базе лежит только цена за квадрат, и с двумя знаками. Введённые «3400 за
  // лист» превращаются в 1142,17 за кв.м, а обратно — в 3400,01: система
  // переписывала набранное число на копейку и выглядела так, будто считает
  // неправильно. Копейки тут нет — есть округление хранения, поэтому из двух
  // одинаковых по смыслу чисел показываем то, которое человек и набрал.
  // Если целое НЕ сходится (цена за кв.м мелкая, лист стоит 297,68), точность
  // настоящая — оставляем как есть.
  const prettify = (derived, toBase, base) => {
    if (derived === "" || derived === undefined) return derived;
    const whole = String(Math.round(Number(derived)));
    return Number(toBase(whole)) === Number(base || 0) ? whole : derived;
  };

  // У рулона пересчёт идёт через ШИРИНУ: погонный метр — это ширина × 1 м.
  const rollWidth = Number(editing?.roll_width) || 0;
  const toPm = (perSqm) => (rollWidth ? round2(Number(perSqm || 0) * rollWidth) : "");
  const fromPm = (perPm) => (rollWidth ? round2(Number(perPm || 0) / rollWidth) : "");

  // Форма материала одной строкой: штучный / лист / рулон. В базе это по-прежнему
  // «считаем в кв.м» (is_roll_material) плюс форма поступления (intake_form).
  const matForm = !editing
    ? "PIECE"
    : editing.is_roll_material
    ? editing.intake_form || "SHEET"
    : "PIECE";
  // Слово для целой единицы — лист или рулон. «Цена за лист» на плёнке, которая
  // приходит рулоном, отвечает не на тот вопрос.
  const wholeUnit = matForm === "ROLL" ? t("warehouse.unitRoll") : t("warehouse.unitSheet");
  function setMatForm(next) {
    // Размер листа принадлежит ТОЛЬКО листу. Оставить его при переключении
    // формы — значит спрятать поле, но не число: у рулона от него считалась бы
    // площадь листа (та самая, из-за которой «Закупка за рулон» показывала
    // цену несуществующего листа).
    // piece_area в базе NOT NULL с нулём по умолчанию — обнуляем, а не занулляем.
    const noSheet = { sheet_width: "", sheet_height: "", piece_area: 0 };
    if (next === "PIECE") {
      setEditing({
        ...editing,
        ...noSheet,
        is_roll_material: false,
        // Единица «кв.м» осталась бы от рулонного и врала бы в подписях цен.
        unit: editing.unit === "SQM" ? "PIECE" : editing.unit,
      });
      return;
    }
    setEditing({
      ...editing,
      ...(next === "ROLL" ? noSheet : {}),
      is_roll_material: true,
      intake_form: next,
      unit: "SQM",
    });
  }

  // Пустое поле — то, куда ещё не вводили: `undefined` у нового материала или
  // стёртая строка. НОЛЬ пустым НЕ считается: у `piece_price` ноль означает
  // «продажа листом недоступна», и подставить туда цену значило бы молча
  // разрешить продажу целым листом там, где её выключили нарочно.
  const blank = (v) => v === undefined || v === null || String(v).trim() === "";

  // Розничная пара — ДВА НЕЗАВИСИМЫХ числа (решение владельца, 2026-08-27).
  // Кв.м куском и кв.м внутри целого листа — разные товары: за кусок платят
  // дороже, потому что обрезок остаётся в цехе, а рез — это работа.
  //
  // Поэтому вторая цена подставляется ТОЛЬКО в пустое поле — то есть один раз,
  // когда материал заводят: набрал цену листа, получил ориентир за кв.м, дальше
  // правишь любую из двух, не оглядываясь на другую.
  //
  // Раньше пара пересчитывалась в обе стороны, пока числа не разойдутся на 2%,
  // и разойтись им было негде: правка цены за кв.м молча переписывала цену
  // листа, а правка цены листа — цену за кв.м. Видно это по каталогу — у ВСЕХ
  // 26 листовых материалов цена листа с точностью до сома равна «за кв.м ×
  // площадь». Разделение существовало в базе и ни разу не сработало на деле.
  function setSqmPrice(v) {
    setEditing({
      ...editing,
      price_per_sqm: v,
      ...(sheetArea && blank(editing?.piece_price) ? { piece_price: toSheet(v) } : {}),
    });
  }

  function setPiecePrice(v) {
    setEditing({
      ...editing,
      piece_price: v,
      ...(sheetArea && blank(editing?.price_per_sqm) ? { price_per_sqm: toSqm(v) } : {}),
    });
  }

  // Разошлась ли пара. Теперь это НОРМА, а не признак ручной скидки, — подпись
  // под ценой листа показывает, во что этот лист обходится по метражу, чтобы
  // две цены можно было сравнить не в уме. Допуск в сом — только на округление.
  const retailPairDiverged = () => {
    const sheet = Number(editing?.piece_price) || 0;
    const derived = Number(toSheet(editing?.price_per_sqm)) || 0;
    if (!sheet || !derived) return false;
    return Math.abs(sheet - derived) > 1;
  };

  // Открытые рулоны — чтобы в строке материала было видно, ИЗ ЧЕГО состоит
  // остаток. «2.9 пог.м» это не один рулон, а початый на 0.9 и целый на 2.0, и
  // себестоимость у них разная в десять раз.
  const [rolls, setRolls] = useState([]);
  // Список рулонов ДОШЁЛ (а не пустой, потому что ещё грузится или не
  // загрузился): пока его нет, судить о расхождении остатка с партиями нельзя.
  const [rollsLoaded, setRollsLoaded] = useState(false);
  // Рулон, который сейчас промеряют рулеткой.
  const [measuring, setMeasuring] = useState(null);
  const loadRolls = () =>
    api
      .get("/warehouse/rolls/", { params: { page_size: 500 } })
      .then((r) => {
        setRolls((r.data.results ?? r.data).filter((x) => Number(x.remaining_area) > 0));
        setRollsLoaded(true);
      })
      .catch(() => {
        setRolls([]);
        setRollsLoaded(false);
      });
  useEffect(() => {
    loadRolls();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [materials.length]);

  // Только НАСТОЯЩИЕ рулоны. У листовой партии ширина тоже задана (1.22 у
  // листа 1.22×2.44), поэтому `metres_remaining` у неё считается и даёт
  // «10.92 метров» — число, которого в природе нет: лист меряют штуками, а не
  // погонными метрами. Фильтр по форме партии, а не по наличию ширины.
  const rollsOf = (m) =>
    !m.sells_by_metre
      ? []
      : rolls
          .filter((r) => r.material === m.id && r.form === "ROLL" && r.metres_remaining != null)
          .sort((a, b) => new Date(a.received_at) - new Date(b.received_at));

  // Сколько кв.м лежит по ВСЕМ партиям материала. У рулонного материала число
  // в карточке обязано с этим сходиться; разошлось — «хвост сверх партий», его
  // не списать ни продажей, ни промером, только свести (см. reconcileLots).
  const lotsAreaOf = (m) =>
    rolls.filter((r) => r.material === m.id).reduce((s, r) => s + Number(r.remaining_area || 0), 0);
  const hasLotsTail = (m) =>
    rollsLoaded && Math.abs(Number(m.quantity || 0) - lotsAreaOf(m)) > 0.0005;

  async function reconcileLots(m) {
    const ok = await confirm(
      t("warehouse.reconcileConfirm", {
        name: m.name,
        qty: qty(m.quantity),
        lots: qty(lotsAreaOf(m).toFixed(4)),
      })
    );
    if (!ok) return;
    try {
      await api.post("/warehouse/materials/reconcile-lots/", { material: m.id });
      toast(t("common.saved"));
      loadRolls();
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  // Промер доступен по каждому рулону отдельно: рулеткой меряют конкретный
  // рулон на полке, а не «материал вообще».
  const measureButtons = (m) =>
    rollsOf(m).map((r) => (
      <button
        key={r.id}
        className="secondary row-btn"
        onClick={() => setMeasuring(r)}
        title={`${r.code || `№${r.id}`} — ${r.metres_remaining} ${t("unit.METER")}`}
      >
        {t("stocktake.button")} {rollsOf(m).length > 1 ? (r.code || `№${r.id}`) : ""}
      </button>
    ));

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
          {m.thickness_mm != null && <span className="muted"> {trim(m.thickness_mm)} {t("unit.MM")}</span>}
          {m.color && <span className="muted">{m.thickness_mm != null ? " · " : " "}{m.color}</span>}
        </span>
      ),
    },
    {
      key: "quantity",
      label: t("common.quantity"),
      render: (m) => (
        <>
          {/* Рулон меряют метрами — в них и показываем. «3,48 кв.м · ≈1 лист»
              для рулона отвечает не на тот вопрос: листа у него нет, а метры
              владелец иначе считает делением в уме. */}
          {m.sells_by_metre && m.metres_remaining != null ? (
            <>
              {qty(m.metres_remaining)} <span className="muted">{t("unit.METER")}</span>
              {/* Из каких рулонов он складывается. Початый идёт первым — его и
                  дожигают, и по нему считается себестоимость следующего реза. */}
              {rollsOf(m).length > 1 && (
                <div className="muted" style={{ fontSize: 12 }}>
                  {t("warehouse.rollsBreakdown", { n: rollsOf(m).length })}:{" "}
                  {rollsOf(m).map((r, i) => (
                    <span key={r.id}>
                      {i > 0 ? " · " : ""}
                      {r.code || `№${r.id}`} — {r.metres_remaining}
                    </span>
                  ))}
                </div>
              )}
            </>
          ) : (
            <>
              {qty(m.quantity)} <span className="muted">{t(`unit.${m.unit}`)}</span>
              {m.sheets_remaining != null && (
                <span className="muted"> · ≈{Math.round(Number(m.sheets_remaining))} {t("warehouse.sheetsShort")}</span>
              )}
            </>
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
    // Порог — тем же форматом, что остаток: «2», а не «2.00» рядом с «0 кв.м».
    { key: "critical_balance", label: t("warehouse.critical"), render: (m) => qty(m.critical_balance) },
    // У закупочной цены единицы не было ВООБЩЕ: «980.00 сом» — за лист или за
    // квадрат? Рядом стояла розничная «1470 сом/кв.м», и две цифры выглядели
    // сравнимыми, хотя без единицы сравнивать их нельзя. Теперь единица есть у
    // обеих, а для листового материала под ценой за кв.м стоит цена листа.
    {
      key: "purchase_price",
      label: t("warehouse.purchasePrice"),
      // У РУЛОНА закуп тоже в погонных метрах: он и покупается метрами, и
      // продаётся метрами. Показывать его в кв.м, да ещё с припиской «за лист»,
      // значило сравнивать несравнимое — рядом стоит розничная в сом/пог.м, и
      // «166.67 сом/кв.м · 400 сом/лист» против «1000 сом/пог.м» не сводится
      // никак. Листа у рулона нет вовсе.
      render: (m) =>
        m.sells_by_metre ? (
          <>
            {/* К БЛИЖАЙШЕМУ, а не вверх: это производная цифра (цена за кв.м
                × ширина), и округление вверх делало из настоящих 200 сом/пог.м
                показные 201 — на цифре, которую владелец сверяет с накладной. */}
            {Math.round(Number(m.purchase_price) * Number(m.roll_width || 0))}{" "}
            <span className="muted">
              {t("warehouse.perUnitShort", { unit: t("unit.METER") })}
            </span>
          </>
        ) : (
          <PriceCell m={m} value={m.purchase_price} t={t} />
        ),
    },
    {
      key: "price_per_unit",
      label: t("warehouse.retailPrice"),
      // У РУЛОНА розничная цена живёт в цене за погонный метр: он продаётся
      // длиной, и `price_per_sqm` у него законно нулевой. Колонка читала только
      // его — и показывала «0 сом/кв.м» на материале с настроенным прайсом.
      render: (m) =>
        m.sells_by_metre ? (
          <>
            {ceilSom(m.price_per_pm)}{" "}
            <span className="muted">
              {t("warehouse.perUnitShort", { unit: t("unit.METER") })}
            </span>
          </>
        ) : (
          <PriceCell
            m={m}
            value={m.is_roll_material ? m.sqm_price : m.price_per_unit}
            pieceValue={m.is_roll_material ? m.piece_price : 0}
            t={t}
          />
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
          {/* Поправить остаток — рядом с приходом, а не в карточке материала:
              «внесли 500 вместо 50» случается прямо здесь, и до сих пор
              исправить это в интерфейсе было нечем. */}
          {/* Промер рулеткой — по каждому рулону. У рулонного материала правка
              общего остатка отвечает не на тот вопрос: меряют конкретный рулон
              на полке, и расхождение должно остаться актом с причиной. Поэтому
              у него кнопки «Остаток» (общее число в кв.м) НЕТ — сервер такую
              правку и не примет: она списывала бы расхождение FIFO со старейшего
              рулона, а не с того, который промеряли. Единственный случай, когда
              число в карточке правится напрямую, — хвост сверх партий, и это
              отдельная кнопка, видимая только когда хвост есть. */}
          {measureButtons(m)}
          {m.sells_by_metre ? (
            hasLotsTail(m) && (
              <button
                className="secondary row-btn"
                onClick={() => reconcileLots(m)}
                title={t("warehouse.reconcileTitle", {
                  qty: qty(m.quantity),
                  lots: qty(lotsAreaOf(m).toFixed(4)),
                })}
              >
                <Icon name="clipboard" size={14} /> {t("warehouse.reconcileLots")}
              </button>
            )
          ) : (
            <button
              className="secondary row-btn"
              onClick={() => setAdjusting(m)}
              title={t("supply.inventory")}
            >
              <Icon name="clipboard" size={14} /> {t("warehouse.fixStock")}
            </button>
          )}
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
        <select value={form} onChange={(e) => setForm(e.target.value)}>
          <option value="">{t("warehouse.allForms")}</option>
          <option value="PIECE">{t("warehouse.formPiece")}</option>
          <option value="SHEET">{t("supply.formSheet")}</option>
          <option value="ROLL">{t("supply.formRoll")}</option>
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

      {measuring && (
        <RollStocktakeModal
          roll={measuring}
          onClose={() => setMeasuring(null)}
          onDone={() => {
            loadRolls();
            load();
          }}
        />
      )}
      {receiving && (
        <ReceiveStockModal
          material={receiving}
          onClose={() => setReceiving(null)}
          // Партии перечитываем вместе с материалами: без этого строка склада
          // сравнивала НОВЫЙ остаток со СТАРЫМ списком партий и показывала
          // несуществующий «хвост сверх партий» с кнопкой «Свести с рулонами».
          // Ложная тревога уходила сама после F5 — то есть выглядела случайной.
          onDone={() => {
            loadRolls();
            load();
          }}
        />
      )}

      {adjusting && (
        <AdjustStockModal
          material={adjusting}
          onClose={() => setAdjusting(null)}
          onDone={() => {
            loadRolls();
            load();
          }}
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
                manageable
                manageTitle={t("warehouse.manageTypes")}
                onCreated={loadRefs}
                onChange={(v) => setEditing({ ...editing, type: v ? Number(v) : null })}
              />
            </div>
            <NumField grow label={t("warehouse.thickness")} value={editing.thickness_mm} onChange={setF("thickness_mm")} />
          </div>

          {/* Артикул из карточки убран: заказчик пишет его прямо в названии
              («ЖЕЛТЫЙ лимон 2,5ММ 237»), и отдельное поле дублировало ту же
              цифру. В базе, в поиске и в сетке массового ввода он остаётся —
              заведённые артикулы не теряются. */}
          <div className="field">
            <label>{t("warehouse.color")}</label>
            <input value={editing.color ?? ""} onChange={(e) => setEditing({ ...editing, color: e.target.value })} />
          </div>

          {/* Размер листа — только у ЛИСТА. У рулона второй стороны нет: он
              продаётся длиной, а ширина у него своя, в поле «Ширина рулона».
              У штучного материала листа нет тем более. Спрашивать высоту у
              рулона — тот же вопрос без ответа, что и заблокированная единица. */}
          {matForm === "SHEET" && (
            <>
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
            </>
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

          {/* Единицу спрашиваем ТОЛЬКО у штучной формы: там она и правда выбор
              — штуки, килограммы или литры. У листа и рулона считается в кв.м
              всегда, и поле стояло заблокированным: вопрос, на который нельзя
              ответить, читается как поломка. Заодно из списка убраны «кв.м» и
              «пог.м» — у штучного материала их выбрать нельзя было и раньше,
              они просто мозолили глаза. */}
          {matForm === "PIECE" && (
            <div className="row">
              <div className="field grow" style={{ margin: 0 }}>
                <label>{t("warehouse.unit")}</label>
                <select
                  value={editing.unit ?? "PIECE"}
                  onChange={(e) => setEditing({ ...editing, unit: e.target.value })}
                >
                  {/* Единица, доставшаяся от старой карточки, из списка не
                      выпадает: иначе поле показало бы пустоту и молча сменило
                      её при сохранении. */}
                  {[...new Set([...PIECE_UNITS, editing.unit || "PIECE"])].map((u) => (
                    <option key={u} value={u}>{t(`unit.${u}`)}</option>
                  ))}
                </select>
              </div>
            </div>
          )}

          {/* Колонка «производство» складской таблицы: откуда возят материал.
              Справочник, а не текст — опечатка иначе заводила бы ещё одно. */}
          <div className="field">
            <label>{t("warehouse.production")}</label>
            <RefSelect
              value={editing.production}
              options={sites}
              endpoint="/warehouse/production-sites/"
              manageable
              manageTitle={t("warehouse.manageSites")}
              onCreated={loadRefs}
              onChange={(v) => setEditing({ ...editing, production: v ? Number(v) : null })}
            />
          </div>

          {/* Форма материала — три кнопки вместо галочки «листовой/рулонный».
              Галочка отвечала на «считаем в кв.м?», но не на «чем он приходит»,
              и форму (лист или рулон) складовщик выбирал заново в каждом
              поступлении. Теперь она задана на материале и подставляется в
              приход: акрил всегда листами, плёнка всегда рулоном. */}
          <div className="field">
            <label>{t("warehouse.stockForm")}</label>
            <div className="tabs" style={{ marginTop: 0 }}>
              {[
                ["PIECE", t("warehouse.formPiece")],
                ["SHEET", t("supply.formSheet")],
                ["ROLL", t("supply.formRoll")],
              ].map(([key, label]) => (
                <button
                  key={key}
                  className={matForm === key ? "active" : ""}
                  onClick={() => setMatForm(key)}
                >
                  {label}
                </button>
              ))}
            </div>
            <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
              {matForm === "PIECE" ? t("warehouse.formPieceHint") : t("warehouse.rollHint")}
            </p>
          </div>

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
                {/* Второе поле пары — в той единице, которой материал ЖИВЁТ.
                    У листа это лист, у рулона — погонный метр: «за рулон
                    целиком» закупку никто не помнит, рулоны приходят разной
                    длины. Раньше у рулона стояло «Закупка, сом/рулон», а
                    считалось оно по площади ЛИСТА (у плёнки она осталась от
                    карточки листа) — цифра не значила ничего. */}
                {/* Поле стоит ВСЕГДА, даже когда пересчитывать не из чего:
                    размер листа (ширину рулона) ещё не ввели. Раньше оно просто
                    не рисовалось, и в «Новом материале» закупки за лист не было
                    вовсе — заказчик решал, что система её не умеет, хотя не
                    хватало одной цифры выше по форме. */}
                {matForm === "ROLL" ? (
                  <NumField
                    grow
                    disabled={!(rollWidth > 0)}
                    placeholder={t("warehouse.needRollWidth")}
                    label={t("warehouse.purchasePerPm")}
                    value={rollWidth > 0
                      ? prettify(toPm(editing.purchase_price), fromPm, editing.purchase_price)
                      : ""}
                    onChange={(v) => setF("purchase_price")(fromPm(v))}
                  />
                ) : (
                  <NumField
                    grow
                    disabled={!(sheetArea > 0)}
                    placeholder={t("warehouse.needSheetSize")}
                    label={t("warehouse.purchasePerSheet", { unit: wholeUnit })}
                    value={sheetArea > 0
                      ? prettify(toSheet(editing.purchase_price), toSqm, editing.purchase_price)
                      : ""}
                    onChange={(v) => setF("purchase_price")(toSqm(v))}
                  />
                )}
              </div>
              {/* Рулон продаётся ДЛИНОЙ: ширина у него не выбор клиента, а
                  свойство товара (ткань 0.9 м режут поперёк на всю ширину).
                  Поэтому у рулонного материала спрашиваем ширину и цену за
                  погонный метр, а не цену за квадрат: владелец держит прайс в
                  метрах, и делить 300 на 0.9 в уме он не станет. */}
              {matForm === "ROLL" ? (
                <>
                  <div className="row">
                    <NumField grow label={`${t("warehouse.rollWidth")} *`} value={editing.roll_width} onChange={setF("roll_width")} />
                    <NumField grow label={t("warehouse.retailPerPm")} value={editing.price_per_pm} onChange={setF("price_per_pm")} />
                  </div>
                  <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                    {t("warehouse.rollWidthHint")}
                  </p>
                  {/* Ширина у рулона обязательна — сервер без неё карточку не
                      сохранит. Раньше пустая ширина проходила, а материал потом
                      МОЛЧА продавался по площади, как лист. Об этом говорим
                      здесь, до кнопки, а не тостом после. */}
                  {!(Number(editing.roll_width) > 0) && (
                    <p style={{ color: "var(--danger)", fontSize: 12, marginTop: -2 }}>
                      {t("warehouse.rollWidthRequired")}
                    </p>
                  )}
                  <div className="row">
                    <NumField grow label={t("pricing.cutRatePm")} value={editing.cut_rate_per_pm} onChange={setF("cut_rate_per_pm")} />
                  </div>
                </>
              ) : (
                <>
                  {/* Две цены продажи стоят ПАРОЙ — ровно как две закупочные
                      строкой выше. Раньше «сом/кв.м» соседствовала со ставкой
                      резки, а «сом/лист» жила отдельным блоком ниже, и в форме
                      получалось две разные «Цены продажи», разнесённые
                      подзаголовком: заказчик находил одну и решал, что второй
                      нет. Величины парные — пусть и стоят парой. */}
                  <div className="row">
                    <NumField grow label={t("warehouse.retailPerSqm")} value={editing.price_per_sqm} onChange={setSqmPrice} />
                    <NumField grow label={t("warehouse.retailPerSheet", { unit: wholeUnit })} value={editing.piece_price} onChange={setPiecePrice} />
                  </div>
                  <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                    {t("warehouse.piecePriceHint", { unit: wholeUnit })}
                  </p>
                  {/* Цены назначены порознь — показываем, во что лист обходится
                      по метражу. Это ответ на «а не продаю ли я лист дешевле,
                      чем тот же метраж кусками»: сравнивать 4376 сом/лист и
                      1700 сом/кв.м в уме, деля на 2,9768, никто не станет. */}
                  {sheetArea > 0 && retailPairDiverged() && (
                    <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                      {t("warehouse.piecePriceDiverged", {
                        perSqm: toSqm(editing.piece_price),
                        unit: wholeUnit,
                      })}
                    </p>
                  )}
                  <div className="row">
                    <NumField grow label={t("pricing.cutRatePm")} value={editing.cut_rate_per_pm} onChange={setF("cut_rate_per_pm")} />
                  </div>
                </>
              )}
              {/* Подсказка про пересчёт по площади ЛИСТА — только у листа.
                  У рулона пара считается по ширине, и площадь листа, оставшаяся
                  в карточке от прежней формы, объясняла бы не ту арифметику. */}
              {matForm !== "ROLL" && sheetArea > 0 && (
                <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                  {t("warehouse.sheetAreaHint", { area: round2(sheetArea) })}
                </p>
              )}
              {/* Размер листа обязателен — сервер без него карточку не сохранит,
                  как и рулон без ширины. Говорим здесь, до кнопки, а не тостом
                  после. Из размера считается и остаток в листах, и закупка за
                  лист: без него обе строки в каталоге просто не показывались. */}
              {matForm === "SHEET" && !(sheetArea > 0) && (
                <p style={{ color: "var(--danger)", fontSize: 12, marginTop: -2 }}>
                  {t("warehouse.sheetSizeRequired")}
                </p>
              )}
              <NumField label={`${t("warehouse.critical")} (кв.м)`} value={editing.critical_balance} onChange={setF("critical_balance")} />

              {/* Опт рулону не нужен: его единица продажи — погонный метр, а
                  «оптом от 5 рулонов» никто не считает. Сама цена за лист
                  переехала наверх, к цене за кв.м: они парные. Здесь остался
                  опт — он про КОЛИЧЕСТВО листов, а не про способ продажи. */}
              {matForm !== "ROLL" && (
                <>
              <SectionLabel>{t("warehouse.wholesaleSection", { unit: wholeUnit })}</SectionLabel>
              <div className="row">
                <NumField grow label={t("warehouse.wholesalePrice", { unit: wholeUnit })} value={editing.wholesale_price} onChange={setF("wholesale_price")} />
                <NumField grow label={t("warehouse.wholesaleMin", { unit: wholeUnit })} value={editing.wholesale_min_qty} onChange={setF("wholesale_min_qty")} />
              </div>
              <p className="muted" style={{ fontSize: 12 }}>{t("warehouse.wholesaleHint")}</p>
                </>
              )}
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
