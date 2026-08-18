import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { useAuth } from "../auth/AuthContext.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { areaOf } from "../utils/area.js";

// Как на сервере (TransactionItem.line_total): каждая строка — вверх до сома.
const ceilSom = (v) => Math.max(0, Math.ceil((Number(v) || 0) - 1e-6));

const EMPTY_CFG = {
  qty: "1",
  width: "",
  length: "",
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
};

/** Configure and append one item (дозаказ) to an existing receipt. */
export default function AddToOrderModal({ receiptId, onClose, onAdded }) {
  const { t } = useTranslation();
  const { toast } = useUI();
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
  // Продажа листом возможна только при цене за лист — иначе только площадь.
  const matMode = matSheet ? (cfg.saleMode === "PIECE" && matPiecePrice > 0 ? "PIECE" : "SQM") : matRoll ? "METER" : null;
  const matArea = matMode === "SQM" ? areaOf(cfg.width, cfg.length) || 0 : 0;
  const matAreaPrice = mat ? Number(mat.sqm_price ?? mat.price_per_sqm ?? mat.price_per_unit ?? 0) : 0;
  const matQty = Number(cfg.qty) || 0;
  const matWholesale =
    matMode === "PIECE" && Number(mat.wholesale_price) > 0 && Number(mat.wholesale_min_qty) > 0 && matQty >= Number(mat.wholesale_min_qty);
  const matPieceUnit = matWholesale ? Number(mat.wholesale_price) : matPiecePrice;
  const wholeUnit = mat && (mat.intake_form || "SHEET") === "ROLL" ? t("warehouse.unitRoll") : t("warehouse.unitSheet");
  // Резка считается по ДЛИНЕ РЕЗА в погонных метрах, а не по площади куска.
  const usesRunM = !!svc?.uses_running_meter;
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
  // Длина реза в погонных метрах: у обычного реза это ОДНА сторона куска
  // («Длина»), у фигурного — то, что ввёл мастер.
  const runM =
    cfg.cutMode === "SIDE" ? Number(cfg.length) || 0 : Number(cfg.running_meters) || 0;

  // Live price preview — по той же формуле, что считает бэкенд: площадь до
  // 0.001 «половиной вверх», каждая строка — вверх до целого сома.
  let preview = 0;
  if (sel?.type === "material") {
    // Цена — по способу продажи: за метр у рулона, за лист или за кв.м у
    // листового, за единицу у штучного. Раньше здесь всегда стояла цена за
    // единицу — у листа она нулевая, и окно молчало о сумме.
    if (matMode === "METER") preview = ceilSom(Number(mat.price_per_pm || 0) * Number(cfg.length || 0));
    else if (matMode === "PIECE") preview = ceilSom(matPieceUnit * matQty);
    else if (matMode === "SQM") preview = ceilSom(matAreaPrice * matArea);
    else preview = ceilSom(Number(sel.obj.price_per_unit) * matQty);
  } else if (svc?.uses_area) {
    const area = areaOf(cfg.width, cfg.length) || 0;
    // Резка: работа = пог.м × ставка, материал = площадь × цена за кв.м. Пока
    // погонные метры не введены, работа = 0 — площадь вместо длины реза давала
    // цену втрое ниже реальной.
    const work = usesRunM ? runM * rate : area * rate;
    preview = ceilSom(work) + ceilSom(area * matSqmPrice);
  } else if (svc?.uses_pieces) preview = ceilSom(Number(svc.rate_per_piece) * Number(cfg.qty || 0));
  else if (svc) preview = ceilSom(Number(svc.base_price) * Number(cfg.qty || 0));

  function buildItem() {
    if (sel.type === "material") {
      const it = { type: "MATERIAL", material: sel.obj.id };
      // Режим — явно, как шлёт касса: сервер его не подставляет.
      if (matMode === "METER") return { ...it, quantity: Number(cfg.length), mode: "METER" };
      if (matMode === "PIECE") return { ...it, quantity: matQty, mode: "PIECE" };
      if (matMode === "SQM") return { ...it, quantity: matArea, mode: "SQM" };
      return { ...it, quantity: matQty };
    }
    if (svc.uses_area) {
      const it = { type: "SERVICE", service: svc.id, material: Number(cfg.materialId), width: Number(cfg.width), length: Number(cfg.length) };
      if (svc.uses_letter_type) it.letter_type = cfg.letter_type;
      if (usesRunM) it.running_meters = runM;
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
  const cutMatPriceMissing = !!(svc && usesArea && cfgMat && !(matSqmPrice > 0));
  const matPriceMissing =
    sel?.type === "material" &&
    (matMode === "METER" ? !(Number(mat.price_per_pm) > 0)
      : matMode === "PIECE" ? !(matPieceUnit > 0)
      : matMode === "SQM" ? !(matAreaPrice > 0)
      : !(Number(sel.obj.price_per_unit) > 0));
  const valid =
    sel &&
    (sel.type === "material"
      ? !matPriceMissing &&
        (matMode === "METER"
          ? Number(cfg.length) > 0
          : matMode === "SQM"
          ? matArea > 0
          : matQty > 0)
      : svc.uses_area
      ? cfg.materialId && Number(cfg.width) > 0 && Number(cfg.length) > 0 && !runMMissing && !rateMissing && !cutMatPriceMissing
      : Number(cfg.qty) > 0);

  async function add() {
    setBusy(true);
    try {
      const { data } = await api.post(`/sales/receipts/${receiptId}/add-items/`, { items: [buildItem()] });
      toast(t("receipts.added"));
      onAdded(data);
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
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
      <div className="field">
        <label>{t("checkout.addItem")}</label>
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
      </div>

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

      {usesArea && (
        <>
          <div className="field">
            <label>{t("checkout.cutMaterial")}</label>
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
          </div>
          <div className="row">
            <div className="field grow"><label>{t("supply.width")}</label><input type="number" step="any" value={cfg.width} onChange={(e) => setCfg({ ...cfg, width: e.target.value })} /></div>
            <div className="field grow"><label>{t("supply.length")}</label><input type="number" step="any" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} /></div>
          </div>
          {/* Как считать длину реза — так же, как в кассе: обычный рез берёт
              одну сторону куска, фигурный ждёт длину кривой от мастера. */}
          {usesRunM && (
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
                    <p style={{ color: "var(--danger)", fontSize: 12, margin: "4px 0 0" }}>
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
          {isAdmin && usesRunM && (
            <div className="field">
              <label>{t("checkout.cutRateLabel")}</label>
              <input
                type="number"
                step="any"
                value={cfg.cutRate}
                onChange={(e) => setCfg({ ...cfg, cutRate: e.target.value })}
                placeholder={String(baseRate)}
              />
            </div>
          )}
          {rateMissing && (
            <p style={{ color: "var(--danger)", fontSize: 12, margin: "0 0 8px" }}>
              {t(isAdmin ? "checkout.rateMissingAdmin" : "checkout.rateMissing")}
            </p>
          )}
          {cutMatPriceMissing && (
            <p style={{ color: "var(--danger)", fontSize: 12, margin: "0 0 8px" }}>{t("checkout.priceMissing")}</p>
          )}
        </>
      )}
      {sel?.type === "material" && matPriceMissing && matMode !== "METER" && (
        <p style={{ color: "var(--danger)", fontSize: 12, margin: "0 0 8px" }}>{t("checkout.priceMissing")}</p>
      )}

      {/* Рулон: одно поле — длина; ширина надписью, режем поперёк на всю. */}
      {matMode === "METER" && (
        <>
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
            {t("checkout.rollWidthFixed", { width: mat.roll_width })}
          </p>
          <div className="field">
            <label>{t("checkout.rollLength")}</label>
            <input type="number" step="any" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} autoFocus />
          </div>
          {!(Number(mat.price_per_pm) > 0) && (
            <p style={{ color: "var(--danger)", fontSize: 13, margin: "0 0 8px" }}>{t("checkout.rollNoPrice")}</p>
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
                <div className="field grow"><label>{t("supply.width")}</label><input type="number" step="any" value={cfg.width} onChange={(e) => setCfg({ ...cfg, width: e.target.value })} /></div>
                <div className="field grow"><label>{t("supply.length")}</label><input type="number" step="any" value={cfg.length} onChange={(e) => setCfg({ ...cfg, length: e.target.value })} /></div>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: -6 }}>
                {t("checkout.sizeHint")}{matArea > 0 ? ` · ${matArea} ${t("unit.SQM")} × ${matAreaPrice}` : ""}
              </p>
            </>
          )}
        </>
      )}

      {sel && !usesArea && !matRoll && !matSheet && (
        <div className="field">
          <label>{sel.type === "service" && svc.uses_pieces ? t("receipts.letters") : t("common.quantity")}</label>
          <input type="number" value={cfg.qty} onChange={(e) => setCfg({ ...cfg, qty: e.target.value })} />
        </div>
      )}

      {sel && preview > 0 && (
        <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
          <div className="crow"><span className="k">{t("checkout.submit")}</span><strong style={{ fontSize: 18 }}>+{preview.toFixed(0)} сом</strong></div>
        </div>
      )}
    </Modal>
  );
}
