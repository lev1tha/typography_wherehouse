import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import CancelPaymentModal from "../../components/CancelPaymentModal.jsx";
import DataTable from "../../components/DataTable.jsx";
import Field from "../../components/Field.jsx";
import LoadError from "../../components/LoadError.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { formatDate, formatMoneyExact as formatMoney } from "../../utils/format.js";
import { useIdempotency } from "../../utils/idempotency.js";

// «Входящие остатки» (волна 2, XL-04/F6/CLI-06): долги и авансы клиентов на
// дату переезда из Excel. Вставка строк прямо из Excel → предпросмотр (найден
// клиент / будет создан / ошибка) → «Провести». Долг на начало — не выручка,
// не налог, не прибыль и не касса, но долг клиента; аванс — без кассы.
// Проводит только админ; ошибочный остаток отменяется, пока по нему ничего не
// оплачено. Ошибочную оплату входящего долга админ отменяет по одной (D-141):
// в кассу — встречная запись сегодня, остаток долга растёт.

const today = () => new Date().toLocaleDateString("sv-SE");
const STATUS_BADGE = { found: "ok", create: "blue", error: "red" };

// Черновик вставки (RU-N9) — в localStorage, как корзина кассы: F5 или уход с
// экрана не стирают вставленное из Excel. Любое обращение — в try/catch: в
// приватном окне хранилище бросает исключение, а экран обязан работать.
const DRAFT_KEY = "chpu.openingDraft.v1";

function loadDraft() {
  try {
    const d = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
    return d && typeof d === "object" && typeof d.text === "string" ? d : null;
  } catch {
    return null;
  }
}

function saveDraft(d) {
  try {
    if (d.text || d.note) localStorage.setItem(DRAFT_KEY, JSON.stringify(d));
    else localStorage.removeItem(DRAFT_KEY);
  } catch {
    /* нет хранилища — без черновика */
  }
}

export default function OpeningBalances() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const idem = useIdempotency();
  const { isAdmin } = useAuth();
  // Отмена одной оплаты входящего долга: {row, payment}.
  const [cancelling, setCancelling] = useState(null);

  const [draft] = useState(loadDraft);
  const [text, setText] = useState(draft?.text || "");
  const [asOf, setAsOf] = useState(draft?.asOf || today());
  const [note, setNote] = useState(draft?.note || "");
  const [plan, setPlan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [rows, setRows] = useState([]);
  const [failed, setFailed] = useState(false);

  function load() {
    setFailed(false);
    api
      .get("/clients/opening-balances/")
      .then((r) => setRows(r.data || []))
      .catch(() => setFailed(true));
  }
  useEffect(load, []);

  // Предпросмотр устаревает, как только вставку поменяли.
  useEffect(() => setPlan(null), [text]);
  useEffect(() => saveDraft({ text, asOf, note }), [text, asOf, note]);

  async function check() {
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post("/clients/opening-balances/preview/", { text });
      setPlan(data);
    } catch (e) {
      setError(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  async function post() {
    if (!plan || plan.totals.errors > 0) return;
    const ok = await confirm(
      t("opening.confirm", {
        debt: formatMoney(plan.totals.debt),
        advance: formatMoney(plan.totals.advance),
        date: formatDate(asOf),
      })
    );
    if (!ok) return;
    setBusy(true);
    setError("");
    try {
      // Ключ повтора (CLI-14): ответ потерялся, нажали ещё раз — те же остатки
      // второй раз не проводятся.
      const body = { text, as_of: asOf, note };
      const { data } = await api.post("/clients/opening-balances/", body, {
        headers: { "Idempotency-Key": idem.keyFor(JSON.stringify(body)) },
      });
      idem.done();
      toast(data.idempotent_replay
        ? t("clients.repeatIgnored")
        : t("opening.done", { n: data.rows.length, clients: data.created_clients }));
      setText("");
      setNote("");
      setPlan(null);
      saveDraft({ text: "", note: "" });
      load();
    } catch (e) {
      idem.failed(e);
      setError(apiError(e, t("common.error")));
    } finally {
      setBusy(false);
    }
  }

  async function revert(row) {
    if (!(await confirm(t("opening.revertAsk", { name: row.client.display_name, sum: formatMoney(row.amount) })))) return;
    try {
      await api.post(`/clients/opening-balances/${row.id}/revert/`);
      toast(t("opening.reverted"));
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const previewColumns = [
    { key: "line", label: "#" },
    { key: "phone", label: t("clients.phone") },
    { key: "name", label: t("opening.name"), render: (r) => r.name || "—" },
    { key: "debt", label: t("opening.debt"), render: (r) => (Number(r.debt) ? formatMoney(r.debt) : "—") },
    { key: "advance", label: t("opening.advance"), render: (r) => (Number(r.advance) ? formatMoney(r.advance) : "—") },
    {
      key: "status",
      label: t("opening.status"),
      render: (r) => (
        <span>
          <span className={`badge ${STATUS_BADGE[r.status] || ""}`}>{t(`opening.status_${r.status}`)}</span>
          {r.client && <span className="muted" style={{ fontSize: 12 }}> {r.client.display_name}</span>}
          {r.errors.map((e) => (
            <span key={e} style={{ display: "block", fontSize: 12, color: "var(--danger-ink)" }}>{e}</span>
          ))}
          {r.warnings.map((w) => (
            <span key={w} className="muted" style={{ display: "block", fontSize: 12 }}>{t(`opening.warn_${w}`)}</span>
          ))}
        </span>
      ),
    },
  ];

  const listColumns = useMemo(
    () => [
      { key: "as_of", label: t("opening.asOf"), render: (r) => formatDate(r.as_of) },
      { key: "client", label: t("checkout.client"), render: (r) => `${r.client.display_name} · ${r.client.phone}` },
      { key: "kind", label: t("opening.kind"), render: (r) => t(`opening.kind_${r.kind}`) },
      { key: "amount", label: t("opening.amount"), render: (r) => formatMoney(r.amount) },
      {
        key: "remaining",
        label: t("opening.remaining"),
        render: (r) => (r.reverted ? <span className="badge warn">{t("opening.revertedBadge")}</span> : formatMoney(r.remaining)),
      },
      {
        // Оплаты входящего долга по одной — с отменой ошибочной (админ, D-141).
        key: "payments",
        label: t("opening.payments"),
        render: (r) =>
          r.kind === "DEBT" && r.payments?.length ? (
            <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {r.payments.map((p) => (
                <span key={p.id} style={{ fontSize: 12 }} className={p.cancelled ? "muted" : ""}>
                  {formatDate(p.paid_on)} · {formatMoney(p.amount)} · {p.method_display}
                  {p.cancelled ? (
                    <span className="badge warn" style={{ marginLeft: 6 }} title={p.cancel_reason || undefined}>
                      {t("opening.paymentCancelled")}
                    </span>
                  ) : isAdmin && !r.reverted ? (
                    <button
                      className="ghost row-btn row-danger"
                      style={{ marginLeft: 6 }}
                      onClick={() => setCancelling({ row: r, payment: p })}
                    >
                      {t("opening.cancelPayment")}
                    </button>
                  ) : null}
                </span>
              ))}
            </span>
          ) : (
            "—"
          ),
      },
      {
        key: "actions",
        label: "",
        render: (r) =>
          isAdmin && !r.reverted && Number(r.remaining) === Number(r.amount) ? (
            <button className="ghost row-btn row-danger" onClick={() => revert(r)}>{t("opening.revert")}</button>
          ) : null,
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t, isAdmin]
  );

  return (
    <>
      <h1>{t("opening.title")}</h1>
      <p className="muted" style={{ fontSize: 13, marginTop: 0, maxWidth: "75ch" }}>{t("opening.hint")}</p>

      <div className="card">
        <Field label={t("opening.paste")} hint={t("opening.pasteHint")}>
          <textarea
            rows={8}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={"0555 11 22 33\tТахир\t12 000\n0700 123 456\tАйбек\t-3 000"}
            style={{ fontFamily: "monospace", width: "100%" }}
          />
        </Field>
        <div className="row" style={{ gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
          <Field style={{ margin: 0 }} label={t("opening.asOf")}>
            <input type="date" value={asOf} max={today()} onChange={(e) => setAsOf(e.target.value)} />
          </Field>
          <Field className="grow" style={{ margin: 0 }} label={t("opening.note")}>
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={255} />
          </Field>
          <button className="secondary" onClick={check} disabled={busy || !text.trim()}>{t("opening.check")}</button>
          <button onClick={post} disabled={busy || !plan || plan.totals.errors > 0 || !plan.rows.length}>
            {t("opening.post")}
          </button>
        </div>
        {error && <div className="callout" role="alert" style={{ marginTop: 10 }}>{error}</div>}
      </div>

      {plan && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3 style={{ marginTop: 0 }}>{t("opening.previewTitle")}</h3>
          <p style={{ fontSize: 13 }}>
            {t("opening.totals", {
              debt: formatMoney(plan.totals.debt),
              advance: formatMoney(plan.totals.advance),
              found: plan.totals.found,
              create: plan.totals.create,
              errors: plan.totals.errors,
            })}
          </p>
          <DataTable columns={previewColumns} rows={plan.rows} rowKey="line"
            rowClass={(r) => (r.status === "error" ? "warn" : "")} />
        </div>
      )}

      <h3 style={{ marginTop: 24 }}>{t("opening.listTitle")}</h3>
      {failed && !rows.length ? (
        <LoadError onRetry={load} />
      ) : (
        <DataTable columns={listColumns} rows={rows} empty={t("opening.empty")}
          rowClass={(r) => (r.reverted ? "row-muted" : "")} />
      )}

      {cancelling && (
        <CancelPaymentModal
          payment={cancelling.payment}
          url={`/clients/opening-balances/${cancelling.row.id}/cancel-payment/`}
          title={t("opening.cancelTitle", { name: cancelling.row.client.display_name })}
          hint={t("opening.cancelHint")}
          onClose={() => setCancelling(null)}
          onDone={() => {
            setCancelling(null);
            load();
          }}
        />
      )}
    </>
  );
}
