import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import ClientPicker from "./ClientPicker.jsx";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { fieldErrors } from "../utils/fieldErrors.js";
import { isCutLine, itemTitle } from "../utils/itemLabel.js";
import { formatMoney, formatNumber } from "../utils/format.js";
import { isCanceled, useLatest } from "../utils/latest.js";
import { applyRules, itemRules } from "../utils/pricingRules.js";

const dayOf = (iso) => (iso ? new Date(iso).toLocaleDateString("sv-SE") : "");
const today = () => new Date().toLocaleDateString("sv-SE");

// Правка ошибочно заведённого чека. Меняются только те поля, которые не двигают
// деньги и склад: наименование, клиент, дата заказа. Состав так не правится —
// пересчёт позиций тянет за собой списание материала, себестоимость по партиям
// FIFO и принятые оплаты; ошибочный состав исправляется удалением и повторным
// вводом. Это же и написано в подсказке внизу, чтобы не искать в документации.
//
// Размеры и работа (STAFF-08): у строки с размерами правятся ширина, длина,
// число деталей, проходы и — у резки — станок. Сервер пересчитывает количество
// и цену по правилам заказа сам, поэтому шлём только то, что поменяли: ушедшее
// «как было» количество перебило бы пересчёт по новым размерам.
//
// Итог и количества до сохранения (S3) — у сервера: та же правка в
// откатываемой транзакции (`edit-items` с `dry_run`). Своя формула окна
// расходилась с сохранённым (≈1 155 при 1 125). Пог.м резки с деталями правятся
// не количеством, а «пог.м на деталь»: количество — оно × детали.
const num = (v) => Number(v) || 0;
const trim = (v) => String(+Number(v).toFixed(4));
const trim3 = (v) => String(+Number(v).toFixed(3));
const int = (v) => Math.max(1, Math.round(Number(v)) || 1);
const MACHINES = ["CNC", "LASER"];
// Цена в окне — цена ДО правил прайса (`catalog_price`): минимум, срочность
// и скидка строки пересчитываются от неё сервером по правилам заказа. У строк,
// проданных до правил, `catalog_price` пуст — их цена и есть цена.
const basePrice = (i) => i.catalog_price ?? i.price_per_item;

export default function EditReceiptModal({ receipt, onClose, onSaved }) {
  const { t } = useTranslation();
  const { toast } = useUI();

  const [title, setTitle] = useState(receipt.title || "");
  const [clientId, setClientId] = useState(receipt.client || "");
  const [orderDate, setOrderDate] = useState(dayOf(receipt.created_at));
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState({});
  const [formError, setFormError] = useState("");

  // Состав чека. Возвращённые строки не показываем: их материал уже вернулся на
  // склад, и правкой количества это не описывается — там был возврат, а не
  // опечатка.
  // Пара реза (перепроверка 10.10, S1 №2): работа с размерами и сразу за ней
  // материал куска — одна позиция. Детали и размеры правятся у работы, а
  // материал сервер пересчитает вместе с ней: {id материала: id работы}.
  const pairWork = (() => {
    const all = receipt.items || [];
    const out = {};
    all.forEach((w, i) => {
      const m = all[i + 1];
      if (
        isCutLine(w) && w.width != null && w.length != null && w.work_material && m &&
        m.type === "MATERIAL" && String(m.material) === String(w.work_material) && m.sale_mode === "SQM" && !m.roll_area
      ) out[m.id] = w.id;
    });
    return out;
  })();
  const [lines, setLines] = useState(() =>
    (receipt.items || [])
      .filter((i) => !i.is_returned)
      .map((i) => ({
        id: i.id,
        pairWork: pairWork[i.id] ?? null,
        name: itemTitle(i),
        unit: i.unit_code ? t(`unit.${i.unit_code}`) : "",
        quantity: String(+Number(i.quantity).toFixed(4)),
        price: String(+Number(basePrice(i)).toFixed(2)),
        rules: i.catalog_price != null ? itemRules(i) : null,
        remove: false,
        // Поля размеров есть только у строк, где они записаны при продаже.
        dims: i.width != null && i.length != null,
        // Изделие из рулона по кв.м (CALC-10): площадь — ширина × длина,
        // правятся размеры, а не площадь; деталей у него нет.
        rollArea: !!i.roll_area,
        work: i.type === "SERVICE",
        cut: isCutLine(i),
        // Длина реза одной детали: пог.м строки ÷ детали.
        runM: trim3(Number(i.quantity) / (i.parts_count || 1)),
        width: i.width != null ? trim3(i.width) : "",
        length: i.length != null ? trim3(i.length) : "",
        parts: String(i.parts_count || 1),
        passes: String(i.passes || 1),
        machine: MACHINES.includes(i.machine) ? i.machine : "",
        // Исполнитель работы (волна 2): кому пойдёт выработка в ведомости.
        executor: i.executor != null ? String(i.executor) : "",
        executorName: i.executor_name || "",
      })),
  );
  // Работающие сотрудники — для выбора исполнителя строки работы.
  const [executors, setExecutors] = useState([]);
  useEffect(() => {
    if (!(receipt.items || []).some((i) => i.type === "SERVICE")) return;
    api.get("/staff/employees/executors/")
      .then((r) => setExecutors(r.data.employees || []))
      .catch(() => {});
  }, [receipt]);

  const setLine = (id, patch) =>
    setLines((ls) => ls.map((l) => (l.id === id ? { ...l, ...patch } : l)));

  // Что сохранено в чеке сейчас — для сравнения с введённым.
  const orig = new Map(
    (receipt.items || []).map((i) => [
      i.id,
      {
        q: String(+Number(i.quantity).toFixed(4)),
        p: String(+Number(basePrice(i)).toFixed(2)),
        width: i.width != null ? trim3(i.width) : "",
        length: i.length != null ? trim3(i.length) : "",
        runM: trim3(Number(i.quantity) / (i.parts_count || 1)),
        parts: String(i.parts_count || 1),
        passes: String(i.passes || 1),
        machine: MACHINES.includes(i.machine) ? i.machine : "",
        executor: i.executor != null ? String(i.executor) : "",
      },
    ]),
  );
  const dimsTouched = (l) => {
    const o = orig.get(l.id);
    return (
      !!o &&
      l.dims &&
      (l.width !== o.width || l.length !== o.length || l.parts !== o.parts ||
        (l.work && l.passes !== o.passes) || (l.cut && (l.machine !== o.machine || l.runM !== o.runM)))
    );
  };

  // Количество и цена строки ПОСЛЕ правки — пока не пришёл ответ сервера. Рез с
  // размерами — пог.м на деталь × детали, площадь — ширина × длина × детали,
  // проходы умножают ставку. Станок меняет ставку — её знает только сервер.
  function afterEdit(l) {
    const o = orig.get(l.id);
    let q = num(l.quantity);
    let p = num(l.price);
    if (l.cut && l.dims) q = Math.round(num(l.runM) * int(l.parts) * 1000 + 1e-7) / 1000;
    if (o && dimsTouched(l)) {
      if (l.quantity === o.q && !l.cut) {
        if (num(l.width) > 0 && num(l.length) > 0) {
          q = Math.round(num(l.width) * num(l.length) * int(l.parts) * 1000 + 1e-7) / 1000;
        }
      }
      if (l.work && l.price === o.p && l.passes !== o.passes) p = (p / int(o.passes)) * int(l.passes);
    }
    // Материал пары реза: площадь — от размеров и деталей строки работы.
    const w = l.pairWork != null ? lines.find((x) => x.id === l.pairWork) : null;
    if (o && w && !w.remove && dimsTouched(w) && l.quantity === o.q && num(w.width) > 0 && num(w.length) > 0) {
      q = Math.round(num(w.width) * num(w.length) * int(w.parts) * 1000 + 1e-7) / 1000;
    }
    return [q, p];
  }

  const localTotal = lines.reduce((s, l) => {
    if (l.remove) return s;
    const [q, p] = afterEdit(l);
    // Строка — вверх до сома, как на сервере (line_total), по своим правилам.
    return s + applyRules(q * p, l.rules || {});
  }, 0);

  // Предпросмотр правки на сервере (S3): с задержкой, последний запрос побеждает.
  const editItems = changedLines();
  const previewKey = editItems.length ? JSON.stringify(editItems) : "";
  const [preview, setPreview] = useState(null); // {key, data} | {key, error}
  const nextPreview = useLatest();
  useEffect(() => {
    if (!previewKey) {
      nextPreview();
      setPreview(null);
      return undefined;
    }
    const id = setTimeout(() => {
      api
        .post(`/sales/receipts/${receipt.id}/edit-items/`, { items: JSON.parse(previewKey), dry_run: true },
          { signal: nextPreview() })
        .then((r) => setPreview({ key: previewKey, data: r.data }))
        .catch((e) => {
          if (isCanceled(e)) return;
          setPreview({ key: previewKey, error: apiError(e, t("common.error")) });
        });
    }, 350);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey, receipt.id]);
  const fresh = preview && preview.key === previewKey && preview.data ? preview.data : null;
  const previewError = preview && preview.key === previewKey ? preview.error : "";
  // Количество строки так, как его сохранит сервер (или оценка окна, пока ждём).
  const serverQty = (id) => {
    const it = fresh?.items?.find((x) => x.id === id);
    return it ? String(+Number(it.quantity).toFixed(4)) : null;
  };
  const computedQty = (l) => serverQty(l.id) ?? String(afterEdit(l)[0]);
  // Поле количества показывает пересчёт, пока его не правили руками; у реза с
  // размерами и изделия из рулона количество только расчётное.
  const shownQty = (l) => {
    if (l.rollArea || (l.cut && l.dims)) return computedQty(l);
    const o = orig.get(l.id);
    return o && l.quantity === o.q ? computedQty(l) : l.quantity;
  };
  const newTotal = fresh
    ? Math.round((Number(fresh.total_price) || 0) - (Number(fresh.refunded_amount) || 0))
    : localTotal;
  const approx = !fresh && lines.some((l) => !l.remove && dimsTouched(l));
  // Сравниваем с тем, что клиенту осталось платить: итог чека держит и
  // возвращённые строки (возврат уменьшает `refunded_amount`, а не итог), а в
  // окне их нет — иначе у чека с частичным возвратом старая сумма всегда
  // стояла бы зачёркнутой.
  const oldTotal = Math.round((Number(receipt.total_price) || 0) - (Number(receipt.refunded_amount) || 0));

  // Что реально изменилось — то и отправляем. Гонять неизменённые строки через
  // склад незачем: каждая правка списывает и возвращает материал заново. Пустые
  // поля в запрос не идут: «не указано» — не «ноль».
  function changedLines() {
    const out = [];
    for (const l of lines) {
      const o = orig.get(l.id);
      if (l.remove) {
        out.push({ id: l.id, remove: true });
        continue;
      }
      const change = { id: l.id };
      // Пог.м резки с размерами — расчётные (пог.м на деталь × детали).
      if ((!o || o.q !== l.quantity) && !(l.cut && l.dims)) {
        if (l.quantity !== "") change.quantity = num(l.quantity);
      }
      if (!o || o.p !== l.price) {
        if (l.price !== "") change.price_per_item = num(l.price);
      }
      if (l.dims && o) {
        if (l.width !== o.width && l.width !== "") change.width = num(l.width);
        if (l.length !== o.length && l.length !== "") change.length = num(l.length);
        if (l.parts !== o.parts && l.parts !== "") change.parts_count = int(l.parts);
        if (l.work && l.passes !== o.passes && l.passes !== "") change.passes = int(l.passes);
        if (l.cut && l.machine !== o.machine && l.machine) change.machine = l.machine;
        if (l.cut && l.runM !== o.runM && l.runM !== "") change.running_meters = num(l.runM);
      }
      if (l.work && o && l.executor !== o.executor) change.executor = l.executor ? Number(l.executor) : null;
      if (Object.keys(change).length > 1) out.push(change);
    }
    return out;
  }

  async function save() {
    setBusy(true);
    setErrors({});
    setFormError("");
    try {
      const items = editItems;
      let data = receipt;
      if (items.length) {
        ({ data } = await api.post(`/sales/receipts/${receipt.id}/edit-items/`, { items }));
      }
      // Шлём только то, что поменяли. Раньше уходили все три поля разом, и
      // правка одного количества переставляла время заказа на полдень (дата
      // «изменилась» на ту же самую) — чек уезжал в хронологии, а журнал
      // действий писал «client, order_date, title» на пустом месте.
      const meta = {};
      if (title.trim() !== (receipt.title || "")) meta.title = title.trim();
      if (String(clientId || "") !== String(receipt.client || "")) meta.client = clientId || null;
      if (orderDate && orderDate !== dayOf(receipt.created_at)) meta.order_date = orderDate;
      if (Object.keys(meta).length) {
        ({ data } = await api.patch(`/sales/receipts/${receipt.id}/`, meta));
      }
      toast(t("receipts.editSaved"));
      onSaved?.(data);
    } catch (e) {
      // Отказ сервера (400) остаётся в окне: тост через 3 секунды не успеешь
      // прочесть, а в тексте названа строка и причина. Сеть и сервер — тостом.
      if (e.response?.status === 400) {
        const fe = fieldErrors(e);
        setErrors(fe);
        if (e.response.data?.detail || !Object.keys(fe).length) setFormError(apiError(e, t("common.error")));
      } else {
        toast(apiError(e, t("common.error")), "error");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("receipts.editTitle", { number: receipt.order_number })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy}>{t("common.save")}</button>
        </>
      }
    >
      {formError && (
        <div className="callout" role="alert">
          <strong>{t("receiptsV2.editRejected")}</strong> {formError}
        </div>
      )}
      <Field label={t("checkout.orderTitle")} error={errors.title}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
      </Field>

      {/* Клиент — поиском по серверу: прежний <select> знал только первых 25
          клиентов, и чек нельзя было переписать на остальных. Показываем
          текущего клиента заказа, даже если его нет в выдаче поиска. */}
      <Field label={t("checkout.client")} error={errors.client}>
        <ClientPicker
          value={clientId ?? ""}
          valueLabel={receipt.client && String(clientId) === String(receipt.client) ? receipt.client_name : undefined}
          noneLabel={t("receipts.noClient")}
          onChange={(id) => setClientId(id)}
        />
      </Field>

      <Field label={t("checkout.orderDate")} hint={t("receipts.editDateHint")} error={errors.order_date}>
        <input
          type="date"
          value={orderDate}
          max={today()}
          onChange={(e) => setOrderDate(e.target.value)}
        />
      </Field>

      {/* Состав заказа: количество и цена строки. Ошибаются не только в
          названии — лишний лист, лишний квадратный метр, цена не та. Склад,
          себестоимость и итог пересчитываются на сервере. */}
      {lines.length > 0 && (
        <div className="field">
          <label>{t("receipts.editItems")}</label>
          {lines.map((l) => (
            <div
              key={l.id}
              style={{
                margin: "0 0 10px",
                paddingBottom: 8,
                borderBottom: "1px solid var(--hairline)",
                opacity: l.remove ? 0.45 : 1,
              }}
            >
              <div className="row" style={{ margin: "0 0 6px", gap: 6, alignItems: "flex-end" }}>
                <Field className="grow" style={{ margin: 0 }} label={l.unit ? `${l.name}, ${l.unit}` : l.name}>
                  <input
                    type="number"
                    step="any"
                    min="0"
                    inputMode="decimal"
                    value={shownQty(l)}
                    disabled={l.remove || l.rollArea || (l.cut && l.dims)}
                    onChange={(e) => setLine(l.id, { quantity: e.target.value })}
                  />
                </Field>
                <Field style={{ margin: 0, width: 110 }} label={t("receipts.editItemPrice")}>
                  <input
                    type="number"
                    step="any"
                    min="0"
                    inputMode="decimal"
                    value={l.price}
                    disabled={l.remove}
                    onChange={(e) => setLine(l.id, { price: e.target.value })}
                  />
                </Field>
                <button
                  className={l.remove ? "secondary row-btn" : "ghost row-btn row-danger"}
                  style={{ marginBottom: 8 }}
                  onClick={() => setLine(l.id, { remove: !l.remove })}
                  title={l.remove ? t("common.cancel") : t("common.delete")}
                >
                  {l.remove ? t("receipts.editItemRestore") : t("common.delete")}
                </button>
              </div>
              {l.pairWork != null && (
                <p className="muted" style={{ fontSize: 12, margin: "0 0 6px" }}>{t("receipts.editPairHint")}</p>
              )}
              {/* Размеры детали: ширина × длина одной детали, сколько деталей,
                  проходов, на каком станке. Количество сервер пересчитает сам. */}
              {l.dims && (
                <div className="row" style={{ margin: 0, gap: 6, alignItems: "flex-end" }}>
                  <Field className="grow" style={{ margin: 0, minWidth: 84 }} label={t("receiptsV2.editWidth")}>
                    <input
                      type="number"
                      step="0.001"
                      min="0"
                      inputMode="decimal"
                      value={l.width}
                      disabled={l.remove}
                      onChange={(e) => setLine(l.id, { width: e.target.value })}
                    />
                  </Field>
                  <Field className="grow" style={{ margin: 0, minWidth: 84 }} label={t("receiptsV2.editLength")}>
                    <input
                      type="number"
                      step="0.001"
                      min="0"
                      inputMode="decimal"
                      value={l.length}
                      disabled={l.remove}
                      onChange={(e) => setLine(l.id, { length: e.target.value })}
                    />
                  </Field>
                  {!l.rollArea && (
                    <Field style={{ margin: 0, width: 84 }} label={t("receiptsV2.editParts")}>
                      <input
                        type="number"
                        step="1"
                        min="1"
                        max="1000"
                        inputMode="numeric"
                        value={l.parts}
                        disabled={l.remove}
                        onChange={(e) => setLine(l.id, { parts: e.target.value })}
                      />
                    </Field>
                  )}
                  {l.cut && (
                    <Field style={{ margin: 0, width: 96 }} label={t("receiptsV2.editRunM")}>
                      <input
                        type="number"
                        step="0.001"
                        min="0"
                        inputMode="decimal"
                        value={l.runM}
                        disabled={l.remove}
                        onChange={(e) => setLine(l.id, { runM: e.target.value })}
                      />
                    </Field>
                  )}
                  {l.work && (
                    <Field style={{ margin: 0, width: 84 }} label={t("receiptsV2.editPasses")}>
                      <input
                        type="number"
                        step="1"
                        min="1"
                        max="20"
                        inputMode="numeric"
                        value={l.passes}
                        disabled={l.remove}
                        onChange={(e) => setLine(l.id, { passes: e.target.value })}
                      />
                    </Field>
                  )}
                  {l.cut && l.machine && (
                    <Field style={{ margin: 0, width: 110 }} label={t("receiptsV2.editMachine")}>
                      <select
                        value={l.machine}
                        disabled={l.remove}
                        onChange={(e) => setLine(l.id, { machine: e.target.value })}
                      >
                        {MACHINES.map((m) => (
                          <option key={m} value={m}>{t(`machine.${m}`)}</option>
                        ))}
                      </select>
                    </Field>
                  )}
                </div>
              )}
              {l.work && (executors.length > 0 || l.executor) && (
                <div className="row" style={{ margin: "6px 0 0", gap: 6, alignItems: "flex-end" }}>
                  <Field className="grow" style={{ margin: 0 }} label={t("receiptsV2.editExecutor")}>
                    <select
                      value={l.executor}
                      disabled={l.remove}
                      onChange={(e) => setLine(l.id, { executor: e.target.value })}
                    >
                      <option value="">{t("checkout2.executorNone")}</option>
                      {l.executor && !executors.some((e) => String(e.id) === l.executor) && (
                        <option value={l.executor}>{l.executorName || `#${l.executor}`}</option>
                      )}
                      {executors.map((e) => (
                        <option key={e.id} value={String(e.id)}>{e.full_name}</option>
                      ))}
                    </select>
                  </Field>
                </div>
              )}
            </div>
          ))}
          <div className="crow">
            <span className="k">{t("common.total")}</span>
            <span>
              {newTotal !== oldTotal && (
                <span className="muted" style={{ textDecoration: "line-through", marginRight: 8 }}>
                  {formatNumber(oldTotal)}
                </span>
              )}
              <strong>{approx ? "≈ " : ""}{formatMoney(newTotal)}</strong>
            </span>
          </div>
          {approx && !previewError && (
            <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("receiptsV2.editServerCalc")}</p>
          )}
          {previewError && (
            <p className="callout" role="status" style={{ fontSize: 13, margin: "6px 0 0" }}>
              <strong>{t("receiptsV2.editRejected")}</strong> {previewError}
            </p>
          )}
          {newTotal < Math.round(Number(receipt.amount_paid) || 0) && (
            <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {t("receipts.editItemsChangeHint", {
                amount: formatNumber(Math.round(Number(receipt.amount_paid) || 0) - newTotal),
              })}
            </p>
          )}
        </div>
      )}

      {lines.some((l) => l.dims) && (
        <p className="muted" style={{ fontSize: 12 }}>{t("receiptsV2.editDimsHint")}</p>
      )}
      <p className="muted" style={{ fontSize: 12 }}>{t("receipts.editItemsHint")}</p>
    </Modal>
  );
}
