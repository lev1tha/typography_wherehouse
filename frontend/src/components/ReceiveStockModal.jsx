import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

// Приём (поступление) нового прихода для КОНКРЕТНОГО материала — открывается с
// его строки в «Складе». Рулонный: сегмент Рулон/Лист + размеры + цена за лист
// (площадь, себестоимость партии и цена/кв.м считаются на лету). Штучный:
// количество (+ факт. закупочная цена). Дёргает те же эндпоинты, что раньше
// делал экран «Поступление».
export default function ReceiveStockModal({ material, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const roll = !!material.is_roll_material;
  // Форма прихода берётся С МАТЕРИАЛА: акрил всегда листами, плёнка всегда
  // рулоном. Раньше окно всегда открывалось на «Рулоне», и на листовом складе
  // (а он у заказчика почти весь листовой) первым делом приходилось
  // переключать вкладку.
  const [form, setForm] = useState(roll ? material.intake_form || "SHEET" : "QTY");
  const [v, setV] = useState({
    width: "", length: "", height: "", sheet_count: "",
    // unit_cost — цена за ОДИН лист (рулон), как её называет поставщик и как она
    // записана в накладной. Себестоимость партии считается из неё умножением:
    // раньше это умножение заказчик делал в уме до того, как открыть окно.
    quantity: "", unit_cost: "", actual_price: "", code: "",
    // Рулон принимают МЕТРАМИ: заявлено поставщиком и намерено по факту, плюс
    // цена за метр — ровно то, что стоит в накладной. Считать 12 000 ÷ 45 в
    // уме владелец не должен.
    declared_length: "", cost_per_pm: "",
    // Дата поступления: заказчик вносит поставки задним числом, когда доходят
    // руки — в его Excel даты идут вразнобой. По ней же выстраивается FIFO.
    received_on: new Date().toISOString().slice(0, 10),
  });
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setV((s) => ({ ...s, [k]: e.target.value }));

  const area = useMemo(() => {
    const w = Number(v.width);
    if (form === "ROLL") {
      const rw = Number(material.roll_width) || w;
      return rw && Number(v.length) ? rw * Number(v.length) : 0;
    }
    if (form === "SHEET")
      return w && Number(v.height) && Number(v.sheet_count) ? w * Number(v.height) * Number(v.sheet_count) : 0;
    return 0;
  }, [form, v.width, v.length, v.height, v.sheet_count]);

  // Партия = цена за штуку × сколько листов. В рулонной форме приходит один
  // рулон, поэтому его цена и есть себестоимость партии.
  const pieces = form === "SHEET" ? Number(v.sheet_count) || 0 : 1;
  // У рулона партию считает цена за МЕТР × принятые метры.
  const batchCost =
    form === "ROLL"
      ? (Number(v.cost_per_pm) > 0 && Number(v.length) > 0
          ? Number(v.cost_per_pm) * Number(v.length)
          : 0)
      : Number(v.unit_cost) > 0 && pieces > 0
      ? Number(v.unit_cost) * pieces
      : 0;
  // Недолив: заявлено минус принято.
  const shortfall =
    Number(v.declared_length) > 0 && Number(v.length) > 0
      ? Number(v.declared_length) - Number(v.length)
      : 0;

  const costPerSqm = area && batchCost ? (batchCost / area).toFixed(2) : null;
  const cur = Number(material.quantity) || 0;
  const unit = t(`unit.${material.unit}`);
  const added = roll ? area : Number(v.quantity) || 0;
  // Слово для целой единицы: лист или рулон — по выбранной вкладке.
  const wholeUnit = form === "ROLL" ? t("warehouse.unitRoll") : t("warehouse.unitSheet");
  // Цена за целую единицу — и старая, и новая. Заказчик покупает листами и
  // цену помнит за лист, а в базе она лежит за кв.м: без этой строки сравнить
  // «почём было» и «почём стало» он мог только в уме.
  // Площадь одной целой единицы: в рулонной форме — сам рулон (ширина×длина),
  // в листовой — лист этой партии, а если размеры ещё не введены, лист из
  // карточки материала. Раньше рулону подставлялась площадь листа, и старая
  // цена показывалась «за рулон» числом, которое к рулону отношения не имело.
  const sheetArea =
    form === "ROLL"
      ? Number(v.width) * Number(v.length) || 0
      : Number(v.width) && Number(v.height)
        ? Number(v.width) * Number(v.height)
        : Number(material.piece_area) || 0;
  const oldPerSqm = Number(material.purchase_price) || 0;
  const perSheet = (perSqm) => (sheetArea > 0 ? Math.round(perSqm * sheetArea) : null);
  const money = (n) => Number(n).toLocaleString("ru-RU");

  const valid = roll
    ? (form === "ROLL"
        ? (material.roll_width || v.width) && v.length
        : v.width && v.height && v.sheet_count) && batchCost > 0
    : !!v.quantity;

  async function submit() {
    setBusy(true);
    try {
      if (roll) {
        await api.post("/warehouse/materials/receive-roll/", {
          material: material.id,
          form,
          code: v.code,
          width: Number(v.width),
          length: form === "ROLL" ? Number(v.length) : null,
          ...(form === "ROLL"
            ? {
                cost_per_pm: Number(v.cost_per_pm) || null,
                declared_length: Number(v.declared_length) || null,
              }
            : {}),
          height: form === "SHEET" ? Number(v.height) : null,
          sheet_count: form === "SHEET" ? Number(v.sheet_count) : null,
          purchase_cost: batchCost,
          received_on: v.received_on || null,
        });
      } else {
        await api.post("/warehouse/materials/supply/", {
          material: material.id,
          quantity: Number(v.quantity),
          actual_price: v.actual_price ? Number(v.actual_price) : null,
          happened_on: v.received_on || null,
          reason: v.code,
        });
      }
      toast(t("supply.done"));
      onDone?.();
      onClose();
    } catch (e) {
      const data = e.response?.data;
      const first = data && (data.detail || (typeof data === "object" ? Object.values(data)[0] : data));
      toast(Array.isArray(first) ? first[0] : first || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  const numField = (label, key, extra) => (
    <div className="field grow" style={{ margin: 0 }}>
      <label>{label}</label>
      <input type="number" step="any" value={v[key]} onChange={set(key)} {...extra} />
    </div>
  );

  return (
    <Modal
      title={`${t("supply.intake")}: ${material.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>{t("supply.intake")}</button>
        </>
      }
    >
      {roll && (
        <>
          {/* Лист впереди рулона: листами приходит почти вся номенклатура. */}
          <div className="tabs" style={{ marginTop: 0 }}>
            {[["SHEET", t("supply.formSheet")], ["ROLL", t("supply.formRoll")]].map(([k, label]) => (
              <button key={k} className={form === k ? "active" : ""} onClick={() => setForm(k)}>
                {label}
              </button>
            ))}
          </div>
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
            {t("supply.intakeFormHint")}
          </p>
        </>
      )}

      {roll && form === "ROLL" && (
        <>
          {/* Ширина берётся из карточки и ЗАМОРАЖИВАЕТСЯ в этой партии: правка
              опечатки в справочнике потом не должна пересчитывать уже принятые
              рулоны. Если у карточки ширины нет — спрашиваем её здесь. */}
          {material.roll_width ? (
            <p className="muted" style={{ fontSize: 13, margin: "0 0 8px" }}>
              {t("supply.widthFromCard", { width: material.roll_width })}
            </p>
          ) : (
            <div className="row">{numField(t("supply.width"), "width", { autoFocus: true })}</div>
          )}
          <div className="row">
            {numField(t("supply.declaredLength"), "declared_length", { autoFocus: !!material.roll_width })}
            {numField(t("supply.acceptedLength"), "length")}
          </div>
          {/* Недолив — на виду сразу, а не «когда-нибудь в отчёте»: рулон за
              рулоном по метру это чистый убыток, который иначе не свести. */}
          {shortfall > 0 && (
            <p style={{ color: "var(--danger)", fontSize: 13, margin: "-4px 0 8px" }}>
              {t("supply.shortfall", { n: shortfall.toFixed(2) })}
            </p>
          )}
        </>
      )}
      {roll && form === "SHEET" && (
        <div className="row">
          {numField(t("supply.width"), "width", { autoFocus: true })}
          {numField(t("supply.height"), "height")}
          {numField(t("supply.sheets"), "sheet_count")}
        </div>
      )}
      {roll && form === "ROLL" && (
        <div className="field" style={{ marginTop: 12 }}>
          <label>{t("supply.costPerPm")}</label>
          <input type="number" step="any" value={v.cost_per_pm} onChange={set("cost_per_pm")} />
          {batchCost > 0 && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
              {t("supply.batchCost")}: {money(Number(v.cost_per_pm))} × {Number(v.length) || 0} ={" "}
              <strong>{money(batchCost)}</strong> сом
            </p>
          )}
        </div>
      )}
      {roll && form !== "ROLL" && (
        <div className="field" style={{ marginTop: 12 }}>
          <label>{t("supply.unitCost", { unit: wholeUnit })}</label>
          <input type="number" step="any" value={v.unit_cost} onChange={set("unit_cost")} />
          {/* Себестоимость партии больше не спрашиваем — показываем, как она
              вышла: «5 × 2 400 = 12 000». Заказчик вводит ровно то число, что
              стоит в накладной поставщика. */}
          {form === "SHEET" && batchCost > 0 && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
              {t("supply.batchCost")}: {pieces} × {money(Number(v.unit_cost))} = <strong>{money(batchCost)}</strong> сом
            </p>
          )}
          {/* Почём материал стоил до этого прихода — рядом, а не в другом
              разделе: приход по новой цене это первое, на что смотрят.
              Сначала за лист — тем же числом, каким его вводят выше. */}
          {oldPerSqm > 0 && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
              {t("supply.currentPrice")}:{" "}
              {perSheet(oldPerSqm) ? `${money(perSheet(oldPerSqm))} ${t("warehouse.perUnitShort", { unit: wholeUnit })} · ` : ""}
              {money(oldPerSqm)} {t("warehouse.perUnitShort", { unit: t("unit.SQM") })}
            </p>
          )}
        </div>
      )}

      {!roll && (
        <>
          <div className="row">
            {numField(`${t("common.quantity")} (${unit})`, "quantity", { autoFocus: true })}
            {numField(t("supply.actualPrice"), "actual_price")}
          </div>
          <p className="muted" style={{ fontSize: 12, margin: "-8px 0 14px" }}>
            {oldPerSqm > 0 ? `${t("supply.currentPrice")}: ${money(oldPerSqm)} ${t("warehouse.perUnitShort", { unit })}. ` : ""}
            {t("supply.priceUnchanged")}
          </p>
        </>
      )}

      <div className="field">
        <label>{t("supply.receivedOn")}</label>
        <input type="date" value={v.received_on} onChange={set("received_on")} />
        <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
          {t("supply.receivedOnHint")}
        </p>
      </div>

      <div className="field">
        <label>{t("supply.rollCode")}</label>
        <input value={v.code} onChange={set("code")} placeholder={t("supply.batchPlaceholder")} />
      </div>

      {added > 0 && (
        <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
          {roll && (
            <>
              <div className="crow"><span className="k">{t("supply.area")}</span><strong>{area.toFixed(2)} {t("unit.SQM")}</strong></div>
              {costPerSqm && (
                <>
                  <div className="crow">
                    <span className="k">{t("supply.costPerSqm")}</span>
                    <strong>
                      {costPerSqm} {t("warehouse.perUnitShort", { unit: t("unit.SQM") })}
                      {/* Насколько подорожал или подешевел этот приход. */}
                      {oldPerSqm > 0 && (
                        <span className="muted" style={{ fontWeight: 400 }}>
                          {" "}({t("supply.priceWas", { value: money(oldPerSqm) })})
                        </span>
                      )}
                    </strong>
                  </div>
                  {/* Цена за целую единицу — тем же числом, каким её называет
                      поставщик: «лист 980», а не «329 за квадрат». */}
                  {perSheet(Number(costPerSqm)) && (
                    <div className="crow">
                      <span className="k">{t("supply.costPerSheet", { unit: wholeUnit })}</span>
                      <strong>
                        {money(perSheet(Number(costPerSqm)))} сом
                        {oldPerSqm > 0 && perSheet(oldPerSqm) && (
                          <span className="muted" style={{ fontWeight: 400 }}>
                            {" "}({t("supply.priceWas", { value: money(perSheet(oldPerSqm)) })})
                          </span>
                        )}
                      </strong>
                    </div>
                  )}
                </>
              )}
            </>
          )}
          <div className="crow">
            <span className="k">{t("supply.becomes")}</span>
            <strong>{cur} → {(cur + added).toFixed(2)} {roll ? t("unit.SQM") : unit}</strong>
          </div>
        </div>
      )}
    </Modal>
  );
}
