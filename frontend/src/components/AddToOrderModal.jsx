import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useAuth } from "../auth/AuthContext.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { areaOf } from "../utils/area.js";
import { formatMoney, formatNumber } from "../utils/format.js";
import { rulesLabel } from "../utils/pricingRules.js";
import Field from "./Field.jsx";

// Как на сервере (TransactionItem.line_total): каждая строка — вверх до сома.
const ceilSom = (v) => Math.max(0, Math.ceil((Number(v) || 0) - 1e-6));
const price = (v) => `${formatNumber(v, { max: 2 })}\u00a0сом`;

const EMPTY_CFG = {
  qty: "1",
  width: "",
  length: "",
  // Деталей одинакового размера и проходов (резка, гравировка): площадь и длина
  // реза умножаются на число деталей, ставка — на число проходов.
  parts: "1",
  passes: "1",
  letter_type: "FLAT",
  materialId: "",
  running_meters: "",
  cutRate: "",
  // Как считать длину реза: обычный рез (одна сторона куска — «Длина») или
  // фигурный (длину кривой вводит мастер). Так же, как в кассе.
  cutMode: "SIDE",
  // Способ продажи материала по площади: лист целиком (PIECE) или кусок по
  // площади (SQM). Рулон — всегда длиной (METER), у него выбора нет. Режим
  // уходит на сервер ЯВНО: без него сервер раньше подставлял «кв.м», и
  // «дозаказать 1 лист» превращалось в 1 кв.м по цене за квадрат.
  saleMode: "PIECE",
  // Рулон: метрами на всю ширину (METER) или по кв.м изделия (SQM, CALC-10) —
  // второй способ есть, только если у рулона задана цена за кв.м.
  rollMode: "METER",
  // Материал клиента (только резка) и комментарий к работе — что резали или
  // гравировали. У такой строки своего материала нет, и без комментария её
  // потом не узнать.
  ownMaterial: false,
  note: "",
  // Отходы: мерка строки. Отходы бывают от любого товара — лист меряют
  // квадратами, рулон метрами, штучное штуками.
  wasteMode: "SQM",
  wasteAmount: "",
};

/** Configure and append one item (дозаказ) to an existing receipt. */
export default function AddToOrderModal({ receiptId, receipt = null, onClose, onAdded }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin } = useAuth();
  const [services, setServices] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [pick, setPick] = useState(""); // "S<id>" | "M<id>"
  const [cfg, setCfg] = useState(EMPTY_CFG);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get("/services/services/").then((r) => setServices(r.data.results.filter((s) => s.is_active !== false)));
    // page_size: без него приходит первая страница из 25 материалов, и
    // остального каталога в списке дозаказа просто нет.
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results));
  }, []);

  // Материал к резке — по площади: лист или кусок. Рулон сюда не идёт: у него
  // нет цены за кв.м, и строка материала уходила бы за 0 сом; сервер такую
  // резку отклоняет. Метры рулона дозаказывают отдельной строкой материала.
  const areaMaterials = materials.filter((m) => m.is_roll_material && !m.sells_by_metre);
  const sel = useMemo(() => {
    if (!pick) return null;
    if (pick[0] === "S") return { type: "service", obj: services.find((s) => s.id === Number(pick.slice(1))) };
    return { type: "material", obj: materials.find((m) => m.id === Number(pick.slice(1))) };
  }, [pick, services, materials]);

  const svc = sel?.type === "service" ? sel.obj : null;
  const usesArea = svc?.uses_area;
  // Материал строкой: рулон — длиной, лист — целиком или по площади, штучный —
  // количеством. Так же, как в кассе; сервер без явного режима лист не примет.
  const mat = sel?.type === "material" ? sel.obj : null;
  const matRoll = !!mat?.sells_by_metre;
  const matSheet = !!mat && !!mat.is_roll_material && !matRoll;
  const matPiecePrice = Number(mat?.piece_price || 0);
  // Рулон с ценой за кв.м изделия (CALC-10) продаётся ещё и по площади
  // изделия: ширина × длина × цена за кв.м, со склада — вся ширина × длина.
  const matRollArea = matRoll && Number(mat?.price_per_sqm) > 0;
  // Продажа листом возможна только при цене за лист — иначе только площадь.
  const matMode = matSheet
    ? (cfg.saleMode === "PIECE" && matPiecePrice > 0 ? "PIECE" : "SQM")
    : matRoll
    ? (matRollArea && cfg.rollMode === "SQM" ? "ROLL_SQM" : "METER")
    : null;
  const matArea = matMode === "SQM" || matMode === "ROLL_SQM" ? areaOf(cfg.width, cfg.length) || 0 : 0;
  const matRollPrice = matMode === "ROLL_SQM" ? Number(mat.price_per_sqm) : 0;
  const matRollTooWide =
    matMode === "ROLL_SQM" && Number(mat.roll_width) > 0 && Number(cfg.width) > Number(mat.roll_width);
  const matAreaPrice = mat ? Number(mat.sqm_price ?? mat.price_per_sqm ?? mat.price_per_unit ?? 0) : 0;
  const matQty = Number(cfg.qty) || 0;
  const matWholesale =
    matMode === "PIECE" && Number(mat.wholesale_price) > 0 && Number(mat.wholesale_min_qty) > 0 && matQty >= Number(mat.wholesale_min_qty);
  const matPieceUnit = matWholesale ? Number(mat.wholesale_price) : matPiecePrice;
  const wholeUnit = mat && (mat.intake_form || "SHEET") === "ROLL" ? t("warehouse.unitRoll") : t("warehouse.unitSheet");
  // Резка считается по ДЛИНЕ РЕЗА в погонных метрах, а не по площади куска.
  const usesRunM = !!svc?.uses_running_meter;
  // Гравировка — площадь × цена за кв.м, материала в строке нет; цену за кв.м
  // здесь вписывает и складовщик (решение владельца: у крупных заказов своя).
  const isEngraving = svc?.kind === "ENGRAVING";
  // Отходы: мерка и цена — свои у каждой строки, склада строка не касается.
  const isWaste = !!svc?.uses_free_measure;
  const wasteMode = cfg.wasteMode;
  const wasteAmount = isWaste
    ? wasteMode === "SQM"
      ? (Number(cfg.width) && Number(cfg.length) ? areaOf(cfg.width, cfg.length) || 0 : Number(cfg.wasteAmount) || 0)
      : Number(cfg.wasteAmount) || 0
    : 0;
  const wasteCatalogue = !isWaste
    ? 0
    : Number(wasteMode === "METER" ? svc.rate_per_pm : wasteMode === "PIECE" ? svc.rate_per_piece : svc.rate_flat) || 0;
  const wasteRate = cfg.cutRate === "" ? wasteCatalogue : Number(cfg.cutRate) || 0;
  const wasteUnit = wasteMode === "METER" ? t("unit.METER") : wasteMode === "PIECE" ? t("unit.PIECE") : t("unit.SQM");
  // Материал клиента: одна строка работы, со склада ничего не уходит, цену
  // резки называют на месте — каталожной у чужого листа нет.
  const ownCut = !!(svc && usesRunM && cfg.ownMaterial);
  const cfgMat = materials.find((m) => m.id === Number(cfg.materialId));
  // Ставка резки — у СТАНКА (ЧПУ / лазер), а если у него своей нет, то у
  // материала, как было до разделения на станки. У прочих площадных услуг — у
  // самой услуги. Админ может перебить ставку на месте, как и в кассе.
  const baseRate = usesRunM
    ? Number(svc?.rate_per_pm) || Number(cfgMat?.cut_rate_per_pm || 0)
    : svc?.uses_letter_type && cfg.letter_type === "VOLUMETRIC"
    ? Number(svc?.rate_volumetric || 0)
    : Number(svc?.rate_flat || 0);
  const rate = cfg.cutRate === "" ? baseRate : Number(cfg.cutRate) || 0;
  const matSqmPrice = cfgMat
    ? Number(cfgMat.sqm_price ?? cfgMat.price_per_sqm ?? cfgMat.price_per_unit ?? 0)
    : 0;
  // Деталей и проходов: у материала клиента деталей нет (размеров куска он не
  // имеет), проходы есть.
  const parts = ownCut ? 1 : Math.max(1, Math.floor(Number(cfg.parts)) || 1);
  const passes = Math.max(1, Math.floor(Number(cfg.passes)) || 1);
  // Площадь всех деталей, один раз до 0.001, как на сервере.
  const partsArea = (w, l) => Math.round((areaOf(w, l, 6) || 0) * parts * 1000 + 1e-7) / 1000;
  // Длина реза в погонных метрах: у обычного реза это ОДНА сторона куска
  // («Длина»), у фигурного — то, что ввёл мастер.
  const runM = ownCut
    ? Number(cfg.running_meters) || 0
    : cfg.cutMode === "SIDE" ? Number(cfg.length) || 0 : Number(cfg.running_meters) || 0;

  // Live price preview — по той же формуле, что считает бэкенд: площадь до
  // 0.001 «половиной вверх», каждая строка — вверх до целого сома.
  let preview = 0;
  if (sel?.type === "material") {
    // Цена — по способу продажи: за метр у рулона, за лист или за кв.м у
    // листового, за единицу у штучного. Раньше здесь всегда стояла цена за
    // единицу — у листа она нулевая, и окно молчало о сумме.
    if (matMode === "METER") preview = ceilSom(Number(mat.price_per_pm || 0) * Number(cfg.length || 0));
    else if (matMode === "ROLL_SQM") preview = ceilSom(matRollPrice * matArea);
    else if (matMode === "PIECE") preview = ceilSom(matPieceUnit * matQty);
    else if (matMode === "SQM") preview = ceilSom(matAreaPrice * matArea);
    else preview = ceilSom(Number(sel.obj.price_per_unit) * matQty);
  } else if (isWaste) {
    preview = ceilSom(wasteAmount * wasteRate);
  } else if (ownCut) {
    // Материал клиента: только работа.
    preview = ceilSom(runM * rate * passes);
  } else if (isEngraving) {
    preview = ceilSom(partsArea(cfg.width, cfg.length) * rate * passes);
  } else if (svc?.uses_area) {
    const area = partsArea(cfg.width, cfg.length);
    // Резка: работа = пог.м × ставка, материал = площадь × цена за кв.м. Пока
    // погонные метры не введены, работа = 0 — площадь вместо длины реза давала
    // цену втрое ниже реальной. Метры вводят на одну деталь — на все детали их
    // умножает сервер; проходы умножают ставку.
    const work = usesRunM ? runM * parts * rate * passes : area * rate * passes;
    preview = ceilSom(work) + ceilSom(area * matSqmPrice);
  } else if (svc?.uses_pieces) preview = ceilSom(Number(svc.rate_per_piece) * Number(cfg.qty || 0));
  else if (svc) preview = ceilSom(Number(svc.base_price) * Number(cfg.qty || 0));

  function buildItem() {
    if (sel.type === "material") {
      const it = { type: "MATERIAL", material: sel.obj.id };
      // Режим — явно, как шлёт касса: сервер его не подставляет.
      if (matMode === "METER") return { ...it, quantity: Number(cfg.length), mode: "METER" };
      // Рулон по кв.м изделия: размеры изделия, площадь считает сервер.
      if (matMode === "ROLL_SQM") return { ...it, mode: "SQM", width: Number(cfg.width), length: Number(cfg.length) };
      if (matMode === "PIECE") return { ...it, quantity: matQty, mode: "PIECE" };
      if (matMode === "SQM") return { ...it, quantity: matArea, mode: "SQM" };
      return { ...it, quantity: matQty };
    }
    if (isWaste) {
      // Мерка — ЯВНО: сервер её не угадывает. Цена тоже всегда своя: на
      // отходы каталожная — лишь подсказка.
      return {
        type: "SERVICE", service: svc.id, mode: wasteMode,
        ...(wasteMode === "SQM" && Number(cfg.width) && Number(cfg.length)
          ? { width: Number(cfg.width), length: Number(cfg.length) }
          : { quantity: wasteAmount }),
        cut_rate: wasteRate, note: cfg.note.trim(),
      };
    }
    if (ownCut) {
      // Цену шлём всегда: она видна и правится в окне у всех, каталожной нет.
      const it = { type: "SERVICE", service: svc.id, own_material: true, running_meters: runM, cut_rate: rate, note: cfg.note.trim() };
      if (passes > 1) it.passes = passes;
      return it;
    }
    if (isEngraving) {
      const it = { type: "SERVICE", service: svc.id, width: Number(cfg.width), length: Number(cfg.length), cut_rate: rate, note: cfg.note.trim() };
      // «Деталей» и «проходов» шлём, только когда они не единица: сервер больше
      // не принимает лишних полей, а единица — и так значение по умолчанию.
      if (parts > 1) it.parts_count = parts;
      if (passes > 1) it.passes = passes;
      return it;
    }
    if (svc.uses_area) {
      // Количества здесь нет намеренно: при размерах сервер его отклоняет —
      // число одинаковых деталей задаёт `parts_count`.
      const it = { type: "SERVICE", service: svc.id, material: Number(cfg.materialId), width: Number(cfg.width), length: Number(cfg.length) };
      if (usesRunM) it.running_meters = runM;
      if (parts > 1) it.parts_count = parts;
      if (passes > 1) it.passes = passes;
      if (isAdmin && cfg.cutRate !== "") it.cut_rate = Number(cfg.cutRate) || 0;
      return it;
    }
    return { type: "SERVICE", service: svc.id, quantity: Number(cfg.qty) };
  }

  // Резка без длины реза не добавляется: пустая длина кривой уезжала бы в чек
  // нулём — материал посчитан, работа бесплатно. Сервер это тоже отклоняет.
  const runMMissing = !!(svc && usesRunM && !(runM > 0));
  // Нулевая ставка/цена без правки руками — пустой каталог, а не скидка: сервер
  // такую строку отклонит. Админ может вписать ставку здесь (0 = подарок),
  // складовщику остаётся позвать админа.
  const rateMissing = !!(svc && usesArea && !(rate > 0) && cfg.cutRate === "");
  const cutMatPriceMissing = !!(svc && usesArea && !ownCut && !isEngraving && cfgMat && !(matSqmPrice > 0));
  const matPriceMissing =
    sel?.type === "material" &&
    (matMode === "METER" ? !(Number(mat.price_per_pm) > 0)
      : matMode === "ROLL_SQM" ? !(matRollPrice > 0)
      : matMode === "PIECE" ? !(matPieceUnit > 0)
      : matMode === "SQM" ? !(matAreaPrice > 0)
      : !(Number(sel.obj.price_per_unit) > 0));
  const valid =
    sel &&
    (sel.type === "material"
      ? !matPriceMissing &&
        (matMode === "METER"
          ? Number(cfg.length) > 0
          : matMode === "ROLL_SQM"
          ? matArea > 0 && !matRollTooWide
          : matMode === "SQM"
          ? matArea > 0
          : matQty > 0)
      : isWaste
      ? wasteAmount > 0 && wasteRate > 0
      : ownCut
      ? runM > 0 && rate > 0
      : isEngraving
      ? Number(cfg.width) > 0 && Number(cfg.length) > 0 && rate > 0
      : svc.uses_area
      ? cfg.materialId && Number(cfg.width) > 0 && Number(cfg.length) > 0 && !runMMissing && !rateMissing && !cutMatPriceMissing
      : Number(cfg.qty) > 0);

  async function add() {
    setBusy(true);
    try {
      const url = `/sales/receipts/${receiptId}/add-items/`;
      const items = [buildItem()];
      let data;
      try {
        ({ data } = await api.post(url, { items }));
      } catch (e) {
        // Сомнительный дозаказ (строка дороже порога, деталь больше листа)
        // сервер не отвергает, а просит подтвердить: показываем его слова и,
        // если «да», повторяем тот же запрос с кодами предупреждений.
        const found = e.response?.status === 409 && e.response.data?.needs_confirmation
          ? e.response.data.warnings
          : null;
        if (!found?.length) throw e;
        const ok = await confirm(
          `${t("receiptsV2.confirmIntro")} ${found.map((w) => w.message).join(" ")}`,
        );
        if (!ok) return;
        ({ data } = await api.post(url, { items, confirmed_warnings: found.map((w) => w.code) }));
      }
      toast(t("receipts.added"));
      // Прайс поменяли, пока заказ был открыт: дозаказ ушёл по новой цене, а
      // прежние строки остались по старой. Говорим об этом сразу, а не оставляем
      // кассира гадать, откуда разница.
      for (const w of data.warnings || []) {
        if (w.code === "price_changed") {
          toast(t("receiptsV2.priceChanged", { name: w.name, was: price(w.was), now: price(w.now) }), "warning");
        }
      }
      onAdded(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("receipts.addToOrder")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={add} disabled={busy || !valid}>{t("common.add")}</button>
        </>
      }
    >
      <Field label={t("checkout.addItem")}>
        <select value={pick} onChange={(e) => { setPick(e.target.value); setCfg({ ...EMPTY_CFG, materialId: areaMaterials[0]?.id ? String(areaMaterials[0].id) : "" }); }}>
          <option value="">—</option>
          <optgroup label={t("checkout.service")}>
            {/* У резки дописываем станок: две услуги резки рядом иначе
                различаются только названием, а думает заказчик станками. */}
            {services.filter((s) => s.is_active !== false).map((s) => (
              <option key={s.id} value={`S${s.id}`}>
                {s.machine_display ? `${s.name} · ${s.machine_display}` : s.name}
              </option>
            ))}
          </optgroup>
          <optgroup label={t("checkout.material")}>
            {materials.map((m) => (
              <option key={m.id} value={`M${m.id}`}>{m.name}</option>
            ))}
          </optgroup>
        </select>
      </Field>

      {svc?.uses_letter_type && (
        <div className="field">
          <label>{t("checkout.letterTypeLabel")}</label>
          <div className="tabs" style={{ margin: 0 }}>
            {["FLAT", "VOLUMETRIC"].map((lt) => (
              <button key={lt} className={cfg.letter_type === lt ? "active" : ""} onClick={() => setCfg({ ...cfg, letter_type: lt })}>
                {t(`letterType.${lt}`)}
              </button>
            ))}
          </div>
        </div>
      )}

      {isWaste && (
        <>
          <p className="muted" style={{ fontSize: 12, margin: "0 0 10px" }}>{t("checkout.wasteHint")}</p>
          <div className="field">
            <label>{t("checkout.wasteMeasure")}</label>
            <div className="tabs" style={{ marginTop: 0 }}>
              {["SQM", "METER", "PIECE"].map((m) => (
                <button
                  key={m}
                  className={wasteMode === m ? "active" : ""}
                  // Цену стираем вместе с меркой: «300 за квадрат» и «300 за
                  // штуку» — разные деньги, и оставить число значило бы
                  // посчитать дозаказ по чужому прайсу.
                  onClick={() => setCfg({ ...cfg, wasteMode: m, cutRate: "", width: "", length: "", wasteAmount: "" })}
                >
                  {t(`checkout.wasteMeasure${m}`)}
                </button>
              ))}
            </div>
          </div>
          {wasteMode === "SQM" ? (
            <>
              <div className="row">
                <Field className="grow" label={t("supply.width")}>
                  <input type="number" step="0.001" value={cfg.width} onChange={(e) => setCfg({ ...cfg, width: e.target.value, wasteAmount: "" })} />
                </Field>
                <Field className="grow" label={t("supply.length")}>
                  <input type="number" step="0.001" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value, wasteAmount: "" })} />
                </Field>
              </div>
              <Field label={t("checkout.wasteAreaDirect")}>
                <input type="number" step="any" value={cfg.wasteAmount} onChange={(e) => setCfg({ ...cfg, wasteAmount: e.target.value, width: "", length: "" })} />
              </Field>
            </>
          ) : (
            <Field label={<>{wasteMode === "METER" ? t("checkout.wasteMetres") : t("checkout.wastePieces")} *</>}>
              <input type="number" step="any" value={cfg.wasteAmount} onChange={(e) => setCfg({ ...cfg, wasteAmount: e.target.value })} />
            </Field>
          )}
          <div className="field">
            <label>{t("checkout.wasteRate", { unit: wasteUnit })} *</label>
            <input
              type="number"
              step="any"
              value={cfg.cutRate}
              onChange={(e) => setCfg({ ...cfg, cutRate: e.target.value })}
              placeholder={String(wasteCatalogue)}
            />
            {!(wasteRate > 0) && (
              <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "4px 0 0" }}>{t("checkout.wasteNeedRate")}</p>
            )}
          </div>
          <Field label={t("checkout.wasteNote")}>
            <input
              value={cfg.note}
              onChange={(e) => setCfg({ ...cfg, note: e.target.value })}
              placeholder={t("checkout.wasteNotePh")}
            />
          </Field>
        </>
      )}

      {usesArea && (
        <>
          {/* Материал клиента: клиент принёс своё, режем и берём только за
              работу. Материал со склада тогда не выбирается вовсе. */}
          {usesRunM && (
            <label className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="checkbox"
                style={{ width: 20, height: 20, minHeight: 0 }}
                checked={!!cfg.ownMaterial}
                onChange={(e) => setCfg({ ...cfg, ownMaterial: e.target.checked, cutMode: "CURVE" })}
              />
              {t("checkout.ownCutCheckbox")}
            </label>
          )}
          {isEngraving && (
            <p className="muted" style={{ fontSize: 12, margin: "0 0 10px" }}>{t("checkout.engravingHint")}</p>
          )}
          {!ownCut && !isEngraving && (
          <Field label={t("checkout.cutMaterial")}>
            <select value={cfg.materialId} onChange={(e) => setCfg({ ...cfg, materialId: e.target.value })}>
              <option value="">—</option>
              {/* Цена за кв.м лежит в sqm_price; price_per_unit у листовых
                  материалов нулевой, и в списке у всех стояло «0.00 сом/кв.м». */}
              {areaMaterials.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({Number(m.sqm_price ?? m.price_per_sqm ?? m.price_per_unit ?? 0)} {t("warehouse.perUnitShort", { unit: t("unit.SQM") })}, {t("dashboard.remaining").toLowerCase()} {m.quantity})
                </option>
              ))}
            </select>
          </Field>
          )}
          {!ownCut && (
          <div className="row">
            <Field className="grow" label={t("supply.width")}>
              <input type="number" step="0.001" value={cfg.width} onChange={(e) => setCfg({ ...cfg, width: e.target.value })} />
            </Field>
            <Field className="grow" label={t("supply.length")}>
              <input type="number" step="0.001" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} />
            </Field>
          </div>
          )}
          {ownCut && (
            <div className="field">
              <label>{t("checkout.ownCutLength")} *</label>
              <input
                type="number"
                step="any"
                value={cfg.running_meters}
                onChange={(e) => setCfg({ ...cfg, running_meters: e.target.value })}
                autoFocus
              />
              {!(runM > 0) && (
                <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "4px 0 0" }}>{t("checkout.ownCutNeedLength")}</p>
              )}
            </div>
          )}
          {/* Деталей одинакового размера и проходов. Размеры выше — ОДНОЙ детали:
              площадь и длина реза умножаются на число деталей, ставка — на число
              проходов. У материала клиента размеров куска нет, деталей тоже. */}
          {(usesRunM || isEngraving) && (
            <div className="row">
              {!ownCut && (
                <Field className="grow" label={t("receiptsV2.parts")} hint={t("receiptsV2.partsHint")}>
                  <input
                    type="number"
                    min="1"
                    max="1000"
                    step="1"
                    inputMode="numeric"
                    value={cfg.parts}
                    onChange={(e) => setCfg({ ...cfg, parts: e.target.value })}
                  />
                </Field>
              )}
              <Field className="grow" label={t("receiptsV2.passes")} hint={t("receiptsV2.passesHint")}>
                <input
                  type="number"
                  min="1"
                  max="20"
                  step="1"
                  inputMode="numeric"
                  value={cfg.passes}
                  onChange={(e) => setCfg({ ...cfg, passes: e.target.value })}
                />
              </Field>
            </div>
          )}
          {/* Как считать длину реза — так же, как в кассе: обычный рез берёт
              одну сторону куска, фигурный ждёт длину кривой от мастера. */}
          {usesRunM && !ownCut && (
            <>
              <div className="field">
                <div className="tabs" style={{ marginTop: 0 }}>
                  {["SIDE", "CURVE"].map((mode) => (
                    <button
                      key={mode}
                      className={cfg.cutMode === mode ? "active" : ""}
                      onClick={() => setCfg({ ...cfg, cutMode: mode })}
                    >
                      {t(`checkout.mode${mode === "SIDE" ? "Side" : "Curve"}`)}
                    </button>
                  ))}
                </div>
              </div>
              {cfg.cutMode === "CURVE" ? (
                <div className="field">
                  <label>{t("checkout.runningMeters")} *</label>
                  <input
                    type="number"
                    step="any"
                    value={cfg.running_meters}
                    onChange={(e) => setCfg({ ...cfg, running_meters: e.target.value })}
                  />
                  <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("checkout.runMetersHint")}</p>
                  {runMMissing && (
                    <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "4px 0 0" }}>
                      {t("checkout.runMetersRequired")}
                    </p>
                  )}
                </div>
              ) : (
                runM > 0 && (
                  <p className="muted" style={{ fontSize: 12, margin: "0 0 12px" }}>
                    {t("checkout.sideAuto", { value: runM })}
                  </p>
                )
              )}
            </>
          )}
          {/* Ставка. У обычной резки её правит только админ; у материала
              клиента и у гравировки — и складовщик (решение владельца). */}
          {((isAdmin && usesRunM) || ownCut || isEngraving) && (
            <Field label={ownCut ? t("checkout.ownCutRate") : isEngraving ? t("checkout.engravingRate") : t("checkout.cutRateLabel")}>
              <input
                type="number"
                step="any"
                value={cfg.cutRate}
                onChange={(e) => setCfg({ ...cfg, cutRate: e.target.value })}
                placeholder={String(baseRate)}
              />
            </Field>
          )}
          {rateMissing && (
            <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "0 0 8px" }}>
              {t(
                ownCut ? "checkout.ownCutNeedRate"
                : isEngraving ? "checkout.engravingNeedRate"
                : isAdmin ? "checkout.rateMissingAdmin" : "checkout.rateMissing"
              )}
            </p>
          )}
          {(ownCut || isEngraving) && (
            <Field label={ownCut ? t("checkout.ownCutNote") : t("checkout.engravingNote")}>
              <input
                value={cfg.note}
                onChange={(e) => setCfg({ ...cfg, note: e.target.value })}
                placeholder={ownCut ? t("checkout.ownCutNotePh") : t("checkout.engravingNotePh")}
              />
            </Field>
          )}
          {cutMatPriceMissing && (
            <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "0 0 8px" }}>{t("checkout.priceMissing")}</p>
          )}
        </>
      )}
      {sel?.type === "material" && matPriceMissing && matMode !== "METER" && matMode !== "ROLL_SQM" && (
        <p style={{ color: "var(--danger-ink)", fontSize: 12, margin: "0 0 8px" }}>{t("checkout.priceMissing")}</p>
      )}

      {/* Рулон: ширина надписью, режем поперёк на всю. Метрами — одно поле,
          длина; по кв.м изделия (CALC-10) — ширина и длина изделия. */}
      {matRoll && (
        <>
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
            {t("checkout.rollWidthFixed", { width: mat.roll_width })}
          </p>
          {matRollArea && (
            <div className="tabs" style={{ marginTop: 0 }} role="group" aria-label={t("rollArea.modeLabel")}>
              {["METER", "SQM"].map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={(cfg.rollMode || "METER") === mode ? "active" : ""}
                  aria-pressed={(cfg.rollMode || "METER") === mode}
                  onClick={() => setCfg({ ...cfg, rollMode: mode })}
                >
                  {t(mode === "SQM" ? "rollArea.modeSqm" : "rollArea.modeMetre")}
                </button>
              ))}
            </div>
          )}
          {matMode === "ROLL_SQM" ? (
            <>
              <div className="row">
                <Field className="grow" label={<>{t("rollArea.width")} *</>}>
                  <input type="number" step="0.001" value={cfg.width} onChange={(e) => setCfg({ ...cfg, width: e.target.value })} autoFocus />
                </Field>
                <Field className="grow" label={<>{t("checkout.rollLength")} *</>}>
                  <input type="number" step="0.001" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} />
                </Field>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                {t("rollArea.hintSqm", { width: mat.roll_width })}
                {matArea > 0 ? ` · ${matArea} ${t("unit.SQM")} × ${matRollPrice}` : ""}
              </p>
              {matRollTooWide && (
                <p style={{ color: "var(--danger-ink)", fontSize: 13, margin: "0 0 8px" }}>
                  {t("checkout.usedWidthTooWide", { width: mat.roll_width })}
                </p>
              )}
            </>
          ) : (
            <>
              <Field label={t("checkout.rollLength")}>
                <input type="number" step="any" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} autoFocus />
              </Field>
              {!(Number(mat.price_per_pm) > 0) && (
                <p style={{ color: "var(--danger-ink)", fontSize: 13, margin: "0 0 8px" }}>{t("checkout.rollNoPrice")}</p>
              )}
            </>
          )}
        </>
      )}

      {/* Лист: целиком (по цене за лист) или кусок по площади — как в кассе. */}
      {matSheet && (
        <>
          <div className="tabs" style={{ marginTop: 0 }}>
            <button
              className={matMode === "PIECE" ? "active" : ""}
              disabled={!(matPiecePrice > 0)}
              title={!(matPiecePrice > 0) ? t("checkout.modePieceNoPrice") : undefined}
              onClick={() => setCfg({ ...cfg, saleMode: "PIECE" })}
            >
              {t("checkout.modePiece")}
            </button>
            <button className={matMode === "SQM" ? "active" : ""} onClick={() => setCfg({ ...cfg, saleMode: "SQM" })}>
              {t("checkout.modeSqm")}
            </button>
          </div>
          {matMode === "PIECE" ? (
            <div className="field">
              <label>{t("common.quantity")} ({wholeUnit})</label>
              <input type="number" value={cfg.qty} onChange={(e) => setCfg({ ...cfg, qty: e.target.value })} />
              {matWholesale && (
                <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("checkout.wholesale")}: {matPieceUnit} × {matQty}</p>
              )}
            </div>
          ) : (
            <>
              <div className="row">
                <Field className="grow" label={t("supply.width")}>
                  <input type="number" step="0.001" value={cfg.width} onChange={(e) => setCfg({ ...cfg, width: e.target.value })} />
                </Field>
                <Field className="grow" label={t("supply.length")}>
                  <input type="number" step="0.001" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} />
                </Field>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                {t("checkout.sizeHint")}{matArea > 0 ? ` · ${matArea} ${t("unit.SQM")} × ${matAreaPrice}` : ""}
              </p>
            </>
          )}
        </>
      )}

      {sel && !usesArea && !isWaste && !matRoll && !matSheet && (
        <Field label={sel.type === "service" && svc.uses_pieces ? t("receipts.letters") : t("common.quantity")}>
          <input type="number" value={cfg.qty} onChange={(e) => setCfg({ ...cfg, qty: e.target.value })} />
        </Field>
      )}

      {sel && preview > 0 && (
        <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
          <div className="crow"><span className="k">{t("checkout.submit")}</span><strong style={{ fontSize: 18 }}>+{formatMoney(preview)}</strong></div>
          {/* Дозаказ считается по правилам заказа (срочность, скидка) — сумма
              выше по каталогу, точную посчитает сервер. */}
          {receipt && rulesLabel(receipt, t) && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
              {t("checkout.addRulesNote", { rules: rulesLabel(receipt, t) })}
            </p>
          )}
        </div>
      )}
    </Modal>
  );
}
