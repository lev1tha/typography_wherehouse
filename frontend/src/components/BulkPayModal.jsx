import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { formatDate, formatMoney, formatNumber } from "../utils/format.js";
import { useIdempotency } from "../utils/idempotency.js";
import Field from "./Field.jsx";

const som = (n) => formatNumber(n);
const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD, местная дата

// Общая выплата: клиент приходит и отдаёт деньги «за всё», а не по одному чеку.
// Одна сумма гасит долги выбранных заказов от старых к новым — так же, как это
// считают на бумаге: сначала то, что висит дольше.
//
// Дату можно поставить задним числом: деньги берут в цехе, а проводят их позже.
export default function BulkPayModal({ client, orders: initialOrders, onClose, onPaid }) {
  const { t } = useTranslation();
  const { toast } = useUI();

  const [orders, setOrders] = useState(initialOrders || []);
  // Входящий долг на дату переезда (волна 2): сервер гасит его ПЕРВЫМ, в списке
  // он тоже первый — id «opening:<id>» уходит вместе с id заказов.
  const [opening, setOpening] = useState([]);
  // `null` = выбраны все — так список не приходится досинхронизировать, когда
  // приезжают заказы за пределами выбранного периода.
  const [picked, setPicked] = useState(null);
  const [amount, setAmount] = useState("");
  const [paidOn, setPaidOn] = useState(today());
  const [method, setMethod] = useState("CASH");
  const [busy, setBusy] = useState(false);
  const idem = useIdempotency();

  // Карточка клиента показывает заказы ВЫБРАННОГО ПЕРИОДА, а долг у неё «на
  // сейчас» — за период его не режут. Поэтому список для выплаты берём заново и
  // без фильтра: иначе долг 5000 предлагалось бы гасить заказами на 2000.
  useEffect(() => {
    api
      .get(`/clients/clients/${client.id}/`)
      .then((r) => {
        setOrders(r.data.orders || []);
        setOpening(
          (r.data.opening_balances || [])
            .filter((b) => b.kind === "DEBT" && Number(b.remaining) > 0)
            .map((b) => ({
              id: `opening:${b.id}`, opening: true, order_number: null,
              title: t("opening.kind_DEBT"), created_at: b.as_of, debt: b.remaining,
            }))
        );
      })
      .catch(() => {});
  }, [client.id]);

  // Заказы с долгом, от старых к новым — в том же порядке их гасит бэкенд.
  const debtors = useMemo(
    () => [
      ...opening,
      ...(orders || [])
        .filter((o) => Number(o.debt) > 0)
        .sort((a, b) => new Date(a.created_at) - new Date(b.created_at)),
    ],
    [orders, opening],
  );

  const chosen = picked === null ? debtors : debtors.filter((o) => picked.includes(o.id));
  const chosenDebt = chosen.reduce((s, o) => s + Math.round(Number(o.debt) || 0), 0);
  // Пустая сумма = закрыть выбранные заказы целиком. Так не приходится вбивать
  // цифру, которую система и так знает.
  const entered = amount === "" ? chosenDebt : Math.round(Number(amount) || 0);
  const valid = chosen.length > 0 && entered > 0;
  const change = Math.max(0, entered - chosenDebt);

  // Как деньги разойдутся по заказам — показываем ДО отправки, чтобы не гадать,
  // какой заказ закроется, а какой останется частично оплаченным.
  const preview = useMemo(() => {
    let left = entered;
    return chosen.map((o) => {
      const debt = Math.round(Number(o.debt) || 0);
      const take = Math.max(0, Math.min(left, debt));
      left -= take;
      return { ...o, take, leftAfter: debt - take };
    });
  }, [chosen, entered]);

  function toggle(id) {
    setPicked((p) => {
      const cur = p === null ? debtors.map((o) => o.id) : p;
      return cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id];
    });
  }

  async function submit() {
    if (!valid) return;
    setBusy(true);
    try {
      const body = {
        // Сумму отправляем только если её ввели: пусто = «закрыть целиком».
        ...(amount === "" ? {} : { amount: entered }),
        receipt_ids: chosen.map((o) => o.id),
        paid_on: paidOn,
        method,
      };
      const { data } = await api.post(`/clients/clients/${client.id}/pay-debt/`, body, {
        headers: { "Idempotency-Key": idem.keyFor(JSON.stringify([client.id, body])) },
      });
      idem.done();
      toast(t("clients.bulkPayDone", { amount: som(data.paid) }));
      onPaid?.(data);
    } catch (e) {
      idem.failed(e);
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("clients.bulkPayTitle")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>
            {t("receipts.acceptShort")}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("clients.bulkPayHint")}</p>

      {debtors.length === 0 ? (
        <p className="muted">{t("clients.noDebtOrders")}</p>
      ) : (
        <>
          <div className="field">
            <label>{t("clients.bulkPayOrders")}</label>
            {debtors.map((o) => {
              const row = preview.find((p) => p.id === o.id);
              return (
                <label
                  key={o.id}
                  className="crow"
                  style={{ cursor: "pointer", alignItems: "center", gap: 8 }}
                >
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                    <input
                      type="checkbox"
                      style={{ width: 18, height: 18, minHeight: 0 }}
                      checked={picked === null || picked.includes(o.id)}
                      onChange={() => toggle(o.id)}
                    />
                    <span>
                      <strong>{o.opening ? t("opening.kind_DEBT") : `№${o.order_number}`}</strong>
                      {o.title && !o.opening ? <span className="muted"> · {o.title}</span> : null}
                      <span className="muted" style={{ fontSize: 12 }}>
                        {" · "}
                        {formatDate(o.created_at)}
                      </span>
                    </span>
                  </span>
                  <span style={{ whiteSpace: "nowrap" }}>
                    {row && row.take > 0 ? (
                      <>
                        <strong style={{ color: "var(--accent-ink)" }}>+{som(row.take)}</strong>
                        {row.leftAfter > 0 && (
                          <span className="muted" style={{ fontSize: 12 }}>
                            {" "}/ {som(row.leftAfter)}
                          </span>
                        )}
                      </>
                    ) : (
                      <span style={{ color: "var(--danger-ink)" }}>{som(o.debt)}</span>
                    )}
                  </span>
                </label>
              );
            })}
          </div>

          <div className="crow">
            <span className="k">{t("clients.bulkPaySelectedDebt")}</span>
            <strong style={{ color: "var(--danger-ink)" }}>{formatMoney(chosenDebt)}</strong>
          </div>

          <Field style={{ marginTop: 10 }} label={t("receipts.payAmount")}>
            {(a) => (
              <>
              <input {...a}
              type="number"
              min="0"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder={String(chosenDebt)}
              autoFocus
            />
              <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("clients.bulkPayAmountHint")}</p>
              </>
            )}
          </Field>

          <div className="row">
            <Field className="grow" style={{ margin: 0 }} label={t("clients.payDate")}>
              <input
                type="date"
                value={paidOn}
                max={today()}
                onChange={(e) => setPaidOn(e.target.value)}
              />
            </Field>
            <Field className="grow" style={{ margin: 0 }} label={t("checkout.paymentMethod")}>
              <select value={method} onChange={(e) => setMethod(e.target.value)}>
                <option value="CASH">{t("checkout.cash")}</option>
                <option value="MBANK">{t("checkout.mbank")}</option>
                <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
              </select>
            </Field>
          </div>
          {paidOn !== today() && (
            <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("clients.backdatedHint")}</p>
          )}

          {change > 0 && (
            <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
              {/* Переплата в долг не идёт — она записывается СДАЧЕЙ на последний
                  погашенный заказ и висит там, пока её не отдадут. Раньше
                  остаток просто показывался числом и нигде не сохранялся. */}
              {t("checkout.change")}:{" "}
              <strong style={{ color: "var(--accent-ink)" }}>{formatMoney(change)}</strong>
              <div style={{ fontSize: 12 }}>{t("checkout.changeHint")}</div>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
