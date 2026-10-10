import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { formatDate, formatMoney, formatNumber } from "../utils/format.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import Pager from "./Pager.jsx";
import { useUI } from "./UIProvider.jsx";

// «Исправить приход» — опечатка в цене или количестве принятой партии.
//
// В Excel это правка ячейки; здесь до 10.10 после первой продажи исправить было
// нечем, и опечатку «обходили» фиктивным списанием, которое портило прибыль.
// Окно сначала показывает, ЧТО изменится (чеки и их маржа, склад, накладная,
// долг поставщику, месяцы), и только потом проводит — одной операцией на
// сервере (`warehouse/lot_correction.py`).
//
// `lot` — нормализованная партия или строка накладной:
//   { roll?, supply_line?, form: ROLL|SHEET|PIECE|QTY, width, height, length,
//     sheet_count, quantity, cost, unit, title }

const som = (n) => formatMoney(n);
const som2 = (n) => formatMoney(n, { fraction: 2 });
const num = (n) => formatNumber(n, { max: 4 });
const PAGE = 8;

/** Чем меряется партия в окне: метры рулона, листы, штуки или кв.м. */
function shapeOf(lot) {
  if (lot.form === "ROLL" && Number(lot.width) > 0) return "ROLL";
  if (lot.form === "SHEET" && Number(lot.width) > 0 && Number(lot.height) > 0) return "SHEET";
  return "QTY";
}

function unitsOf(shape, v) {
  if (shape === "ROLL") return Number(v.length) || 0;
  if (shape === "SHEET") return Number(v.sheet_count) || 0;
  return Number(v.quantity) || 0;
}

export default function LotCorrectionModal({ lot, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const shape = shapeOf(lot);
  const initial = {
    width: lot.width ?? "",
    height: lot.height ?? "",
    length: lot.length ?? "",
    sheet_count: lot.sheet_count ?? "",
    quantity: lot.quantity ?? "",
  };
  const units0 = unitsOf(shape, initial) || 1;
  const [v, setV] = useState(initial);
  const [price, setPrice] = useState(String(+(Number(lot.cost) / units0).toFixed(2)));
  const [total, setTotal] = useState(String(Number(lot.cost)));
  // Что правили последним — цену единицы или сумму: второе считается.
  const [driver, setDriver] = useState("price");
  const [plan, setPlan] = useState(null);
  const [planKey, setPlanKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(1);

  const unitLabel =
    shape === "ROLL" ? t("unit.METER") : shape === "SHEET" ? t("warehouse.sheetsShort") : lot.unit;
  // «Цена за лист», а не «за лист.»: в подписи цены — целое слово.
  const priceUnit = shape === "SHEET" ? t("warehouse.unitSheet") : unitLabel;
  const units = unitsOf(shape, v);
  const totalNow = driver === "price" ? +(Number(price) * units).toFixed(2) : Number(total);
  const priceNow = driver === "price" ? Number(price) : units ? Number(total) / units : 0;

  const payload = useMemo(() => {
    const body = lot.roll ? { roll: lot.roll } : { supply_line: lot.supply_line };
    const fields = shape === "ROLL" ? ["length"] : shape === "SHEET" ? ["width", "height", "sheet_count"] : ["quantity"];
    fields.forEach((k) => {
      if (String(v[k]) !== String(initial[k]) && Number(v[k]) !== Number(initial[k])) body[k] = v[k];
    });
    if (Math.abs(totalNow - Number(lot.cost)) >= 0.005) body.purchase_cost = totalNow.toFixed(2);
    return body;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [v, totalNow]);
  const key = JSON.stringify(payload);
  const fresh = plan && planKey === key;
  const blocked = fresh && (plan.closed_months.length > 0 || plan.warnings.some((w) => w.code === "journal_ambiguous"));
  const invalid = units <= 0 || !(totalNow >= 0) || Object.keys(payload).length <= 1;

  const set = (k) => (e) => {
    setV((prev) => ({ ...prev, [k]: e.target.value }));
    setError("");
  };

  async function check() {
    if (invalid || busy) return;
    setBusy(true);
    setError("");
    try {
      const r = await api.post("/warehouse/lot-correction/preview/", payload);
      setPlan(r.data);
      setPlanKey(key);
      setPage(1);
    } catch (e) {
      setPlan(null);
      setError(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!fresh || blocked || busy) return;
    setBusy(true);
    try {
      await api.post("/warehouse/lot-correction/apply/", payload);
      toast(t("lotFix.done"));
      onDone?.();
      onClose();
    } catch (e) {
      setError(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  const arrow = (a, b, fmt = som) => (
    <span>
      {fmt(a)} → <strong>{fmt(b)}</strong>
    </span>
  );
  const delta = (n) => {
    const x = Number(n);
    if (!x) return null;
    return (
      <span className="muted" style={{ marginLeft: 6 }}>
        ({x > 0 ? "+" : ""}{som2(x)})
      </span>
    );
  };

  const receipts = plan?.receipts || [];
  const shown = receipts.slice((page - 1) * PAGE, page * PAGE);

  return (
    <Modal
      wide
      title={t("lotFix.title")}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          {!fresh ? (
            <button onClick={check} disabled={busy || invalid}>
              {busy ? t("common.loading") : t("lotFix.preview")}
            </button>
          ) : (
            <button className="danger" onClick={submit} disabled={busy || blocked}>
              {busy ? t("common.loading") : t("lotFix.apply")}
            </button>
          )}
        </>
      }
    >
      <p style={{ marginTop: 0 }}>
        <strong>{lot.title}</strong>
      </p>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("lotFix.hint")}</p>

      <div className="row">
        {shape === "ROLL" && (
          <>
            <Field className="grow" label={t("lotFix.width")} hint={t("lotFix.widthFixed")}>
              <input type="number" value={v.width} disabled />
            </Field>
            <Field className="grow" label={t("lotFix.length")}>
              <input type="number" step="any" min="0" value={v.length} onChange={set("length")} autoFocus />
            </Field>
          </>
        )}
        {shape === "SHEET" && (
          <>
            <Field className="grow" label={t("lotFix.width")}>
              <input type="number" step="any" min="0" value={v.width} onChange={set("width")} />
            </Field>
            <Field className="grow" label={t("lotFix.height")}>
              <input type="number" step="any" min="0" value={v.height} onChange={set("height")} />
            </Field>
            <Field className="grow" label={t("lotFix.sheets")}>
              <input type="number" step="any" min="0" value={v.sheet_count} onChange={set("sheet_count")} autoFocus />
            </Field>
          </>
        )}
        {shape === "QTY" && (
          <Field className="grow" label={`${t("common.quantity")} (${unitLabel})`}>
            <input type="number" step="any" min="0" value={v.quantity} onChange={set("quantity")} autoFocus />
          </Field>
        )}
      </div>
      <div className="row">
        <Field className="grow" label={t("lotFix.unitPrice", { unit: priceUnit })}>
          <input
            type="number"
            step="any"
            min="0"
            value={driver === "price" ? price : +priceNow.toFixed(2)}
            onChange={(e) => {
              setDriver("price");
              setPrice(e.target.value);
              setError("");
            }}
          />
        </Field>
        <Field className="grow" label={t("lotFix.total")}>
          <input
            type="number"
            step="any"
            min="0"
            value={driver === "total" ? total : totalNow}
            onChange={(e) => {
              setDriver("total");
              setTotal(e.target.value);
              setError("");
            }}
          />
        </Field>
      </div>
      <div className="muted" style={{ fontSize: 13 }}>
        {t("lotFix.was")}: {num(unitsOf(shape, initial))} {unitLabel} · {som2(lot.cost)}
      </div>

      {error && (
        <p className="field-error" role="alert" style={{ marginTop: 10 }}>{error}</p>
      )}

      {fresh && (
        <section aria-live="polite" style={{ marginTop: 14 }}>
          <h3 style={{ margin: "0 0 6px" }}>{t("lotFix.whatChanges")}</h3>
          {plan.closed_months.length > 0 && (
            <div className="callout" role="alert" style={{ borderColor: "var(--danger-line)" }}>
              <strong style={{ color: "var(--danger-ink)" }}>{t("lotFix.closed")}</strong>{" "}
              {plan.closed_months.map((m) => m.split("-").reverse().join(".")).join(", ")}
            </div>
          )}
          {plan.warnings.map((w) => (
            <div key={w.code} className="callout">{w.message}</div>
          ))}
          <div className="crow">
            <span className="k">{t("lotFix.quantity")}</span>
            {arrow(plan.before.units, plan.after.units, (n) => `${num(n)} ${unitLabel}`)}
          </div>
          <div className="crow">
            <span className="k">{t("lotFix.lotCost")}</span>
            {arrow(plan.before.purchase_cost, plan.after.purchase_cost, som2)}
          </div>
          <div className="crow">
            <span className="k">{t("lotFix.stockValue")}</span>
            <span>
              {arrow(plan.stock.value_before, plan.stock.value_after, som2)}
              {delta(plan.stock.value_delta)}
            </span>
          </div>
          {plan.supply && (
            <>
              <div className="crow">
                <span className="k">{t("lotFix.supplyTotal", { n: plan.supply.number || `#${plan.supply.id}` })}</span>
                {arrow(plan.supply.total_before, plan.supply.total_after, som2)}
              </div>
              <div className="crow">
                <span className="k">{t("lotFix.supplierDebt")}</span>
                <span>
                  {arrow(plan.supply.debt_before, plan.supply.debt_after, som2)}{" "}
                  <span className="muted">({t("lotFix.paid")}: {som2(plan.supply.paid)})</span>
                </span>
              </div>
              {Number(plan.supply.overpaid_after) > 0 && (
                <div className="crow">
                  <span className="k">{t("lotFix.overpaid")}</span>
                  <strong style={{ color: "var(--amber-ink)" }}>{som2(plan.supply.overpaid_after)}</strong>
                </div>
              )}
            </>
          )}
          {plan.lot_debt && plan.lot_debt.tracked && (
            <div className="crow">
              <span className="k">{t("lotFix.supplierDebt")}</span>
              <span>
                {arrow(plan.lot_debt.debt_before, plan.lot_debt.debt_after, som2)}
                {Number(plan.lot_debt.overpaid_after) > 0 && (
                  <span style={{ color: "var(--amber-ink)", marginLeft: 6 }}>
                    {t("lotFix.overpaid")}: {som2(plan.lot_debt.overpaid_after)}
                  </span>
                )}
              </span>
            </div>
          )}
          <div className="crow">
            <span className="k">{t("lotFix.cogs")}</span>
            <span>
              {Number(plan.cogs_delta) ? `${Number(plan.cogs_delta) > 0 ? "+" : ""}${som2(plan.cogs_delta)}` : t("lotFix.noChange")}
            </span>
          </div>
          {plan.months.length > 0 && (
            <div className="crow">
              <span className="k">{t("lotFix.months")}</span>
              <span>{plan.months.map((m) => m.split("-").reverse().join(".")).join(", ")}</span>
            </div>
          )}

          {receipts.length > 0 && (
            <div className="table-scroll" style={{ marginTop: 10 }}>
              <table className="table plain-table">
                <thead>
                  <tr>
                    <th>{t("lotFix.order")}</th>
                    <th>{t("lotFix.date")}</th>
                    <th>{t("lotFix.costCol")}</th>
                    <th>{t("lotFix.marginCol")}</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => (
                    <tr key={r.id}>
                      <td>№{r.order_number}{r.items.some((i) => i.how === "inferred") ? " *" : ""}</td>
                      <td>{formatDate(r.date)}</td>
                      <td>{arrow(r.cost_before, r.cost_after, som)}</td>
                      <td>
                        <span style={{ color: Number(r.margin_after) < 0 ? "var(--danger-ink)" : undefined }}>
                          {arrow(r.margin_before, r.margin_after, som)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <Pager page={page} count={receipts.length} pageSize={PAGE} onPage={setPage} />
            </div>
          )}
          {plan.inferred_count > 0 && (
            <p className="muted" style={{ fontSize: 12 }}>* {t("lotFix.inferred", { n: plan.inferred_count })}</p>
          )}
          {plan.legacy_count > 0 && (
            <div className="callout">
              {t("lotFix.legacy", { n: plan.legacy_count })}{" "}
              <span className="muted">
                {plan.legacy.slice(0, 20).map((x) => `№${x.order_number}`).join(", ")}
                {plan.legacy.length > 20 ? " …" : ""}
              </span>
            </div>
          )}
          <p className="muted" style={{ fontSize: 12 }}>{t("lotFix.notALoss")}</p>
        </section>
      )}
    </Modal>
  );
}
