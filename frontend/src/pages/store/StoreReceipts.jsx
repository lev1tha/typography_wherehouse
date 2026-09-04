import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import api from "../../api/api.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import AddToOrderModal from "../../components/AddToOrderModal.jsx";
import RefundModal from "../../components/RefundModal.jsx";
import { itemTitle } from "../../utils/itemLabel.js";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const trimQty = (n) => String(+Number(n || 0).toFixed(3));
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import PayDebtModal from "../../components/PayDebtModal.jsx";
import PrintDocs from "../../components/PrintDocs.jsx";
import { FulfillmentBadge, PaymentBadge } from "../../components/StatusBadge.jsx";
import { useUI } from "../../components/UIProvider.jsx";

export default function StoreReceipts() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [stats, setStats] = useState(null);
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(null);
  const [printing, setPrinting] = useState(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [advancingId, setAdvancingId] = useState(null);
  const { isAdmin } = useAuth();
  const [paying, setPaying] = useState(null);
  const [sort, setSort] = useState({ key: "_debt", dir: "desc" });

  function orderingParam() {
    const tail = sort.key !== "created_at" ? ",-created_at" : "";
    return (sort.dir === "desc" ? "-" : "") + sort.key + tail;
  }

  function onSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" }));
  }

  const nextShort = (s) => (s === "PROCESSING" ? t("receipts.toReady") : t("receipts.toIssued"));

  // Шаг назад по производству. Нужен только для ошибочного нажатия: вперёд
  // заказ идёт сам, а назад его возвращают, когда готовность или выдачу
  // отметили раньше времени. Из «Готовится» назад некуда — кнопки там нет.
  const PREV = { ISSUED: "READY", READY: "PROCESSING" };
  const backShort = (s) =>
    PREV[s] === "READY" ? t("receipts.toReady") : t("receipts.toProcessing");

  async function move(r, status, e) {
    e?.stopPropagation();
    setAdvancingId(r.id);
    try {
      await api.post(`/sales/receipts/${r.id}/set-fulfillment/`, { status });
      load();
      toast(t("receipts.statusUpdated"));
    } catch (err) {
      toast(err?.response?.data?.detail || t("common.error"), "error");
    } finally {
      setAdvancingId(null);
    }
  }

  const advance = (r, e) =>
    move(r, r.fulfillment_status === "PROCESSING" ? "READY" : "ISSUED", e);
  const rollback = (r, e) => move(r, PREV[r.fulfillment_status], e);

  async function undoPay(r, e) {
    e?.stopPropagation();
    if (!(await confirm(t("receipts.confirmUnpay")))) return;
    try {
      const { data } = await api.post(`/sales/receipts/${r.id}/unpay/`, {});
      if (open && open.id === data.id) setOpen(data);
      load();
      toast(t("receipts.unpayDone"));
    } catch (err) {
      toast(err.response?.data?.detail || t("common.error"), "error");
    }
  }

  function load() {
    const params = search ? { search } : {};
    api.get("/sales/receipts/", { params: { ...params, ordering: orderingParam() } }).then((r) => setRows(r.data.results));
    api.get("/sales/receipts/stats/", { params }).then((r) => setStats(r.data));
  }
  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, sort]);

  // Возврат — в отдельном окне с выбором позиций (целиком по умолчанию);
  // раньше здесь был только возврат всего чека одним подтверждением.
  const [refunding, setRefunding] = useState(false);

  async function setFulfillment(status) {
    setBusy(true);
    try {
      const { data } = await api.post(
        `/sales/receipts/${open.id}/set-fulfillment/`, { status }
      );
      setOpen(data);
      load();
      toast(t("receipts.statusUpdated"));
    } catch (err) {
      toast(err?.response?.data?.detail || t("common.error"), "error");
    } finally {
      setBusy(false);
    }
  }

  const columns = [
    {
      key: "order_number",
      label: t("receipts.number"),
      render: (r) => (
        <>
          <strong>№{r.order_number ?? "—"}</strong>
          {r.title ? <div className="muted" style={{ fontSize: 12 }}>{r.title}</div> : null}
        </>
      ),
    },
    {
      key: "payment_status",
      label: t("receipts.status"),
      render: (r) => <PaymentBadge status={r.payment_status} />,
    },
    {
      key: "payment_method",
      label: t("receipts.method"),
      render: (r) => t(`checkout.${r.payment_method.toLowerCase()}`),
    },
    {
      key: "fulfillment",
      label: t("receipts.fulfillment"),
      render: (r) =>
        r.has_service ? (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            <FulfillmentBadge status={r.fulfillment_status} />
            {PREV[r.fulfillment_status] && (
              <button
                className="secondary"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
                disabled={advancingId === r.id}
                onClick={(e) => rollback(r, e)}
                title={t("receipts.rollbackTitle")}
              >
                ← {backShort(r.fulfillment_status)}
              </button>
            )}
            {r.fulfillment_status !== "ISSUED" && (
              <button
                className="secondary"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
                disabled={advancingId === r.id}
                onClick={(e) => advance(r, e)}
                title={nextShort(r.fulfillment_status)}
              >
                → {nextShort(r.fulfillment_status)}
              </button>
            )}
          </div>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { key: "total_price", label: t("common.total"), sortKey: "total_price", render: (r) => som(r.total_price) },
    {
      key: "debt",
      label: t("receipts.debt"),
      sortKey: "_debt",
      render: (r) => {
        const hasDebt = Number(r.debt) > 0;
        // Принимать оплату и откатывать её может только админ — складовщик
        // видит долг, но кнопок у него нет (бэкенд тоже вернёт 403).
        const canUndo =
          isAdmin &&
          (r.payment_status === "PAID" || Number(r.amount_paid) > 0) &&
          !["REFUNDED", "PARTIALLY_REFUNDED"].includes(r.payment_status) &&
          r.status !== "CANCELLED";
        if (!hasDebt && !canUndo) return <span className="muted">0</span>;
        return (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            {hasDebt && <span style={{ color: "var(--danger)", fontWeight: 600 }}>{som(r.debt)}</span>}
            {hasDebt && isAdmin && (
              <button
                className="secondary"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
                onClick={(e) => { e.stopPropagation(); setPaying(r); }}
              >
                {t("receipts.pay")}
              </button>
            )}
            {canUndo && (
              <button
                className="ghost"
                style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap", color: "var(--ink-muted)" }}
                onClick={(e) => undoPay(r, e)}
                title={t("receipts.unpay")}
              >
                ↩ {t("receipts.unpayShort")}
              </button>
            )}
          </div>
        );
      },
    },
    {
      key: "created_at",
      label: t("receipts.date"),
      sortKey: "created_at",
      render: (r) => new Date(r.created_at).toLocaleString("ru-RU"),
    },
    {
      key: "actions",
      label: t("common.actions"),
      render: (r) => (
        <div className="row" style={{ gap: 6, alignItems: "center", margin: 0, flexWrap: "nowrap" }}>
          {/* Повторный заказ складовщик оформляет чаще админа — он и стоит за
              кассой. Состав переносится, цены берутся сегодняшние. */}
          <button
            className="secondary row-btn"
            onClick={(e) => { e.stopPropagation(); navigate(`/app/checkout?repeat=${r.id}`); }}
            title={t("receipts.repeatHint")}
          >
            <Icon name="undo" size={14} /> {t("receipts.repeat")}
          </button>
          {/* Накладную и товарный чек выдаёт складовщик — печать нужна ему
              не меньше, чем админу. */}
          <button
            className="secondary row-btn"
            onClick={(e) => { e.stopPropagation(); setPrinting(r); }}
            title={t("print.title")}
          >
            <Icon name="printer" size={14} /> {t("print.print")}
          </button>
          <button className="ghost" onClick={() => setOpen(r)} aria-label={t("common.edit")}>
            <Icon name="arrow-right" size={18} />
          </button>
        </div>
      ),
    },
  ];

  const canRefund =
    open &&
    !["REFUNDED", "CANCELLED"].includes(open.payment_status) &&
    open.status !== "CANCELLED" &&
    (open.items || []).some((i) => !i.is_returned);
  const canEdit = open && open.payment_status !== "REFUNDED" && open.status !== "CANCELLED";

  return (
    <>
      <h1>{t("receipts.title")}</h1>
      {stats && (
        <div className="stat-grid" style={{ marginBottom: 16 }}>
          <div className="stat"><div className="label">{t("receipts.statTotal")}</div><div className="value">{stats.total}</div></div>
          <div className="stat"><div className="label">{t("receipts.statWorking")}</div><div className="value">{stats.working}</div></div>
          <div className="stat"><div className="label">{t("receipts.statReady")}</div><div className="value">{stats.ready}</div></div>
          <div className="stat">
            <div className="label">{t("receipts.debt")}</div>
            <div className="value" style={Number(stats.debt) > 0 ? { color: "var(--danger)" } : undefined}>
              {Math.round(Number(stats.debt)).toLocaleString("ru-RU")} сом
            </div>
          </div>
        </div>
      )}
      <div className="toolbar">
        <input
          className="search"
          placeholder={`${t("common.search")} (${t("receipts.number")})`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <DataTable columns={columns} rows={rows} sort={sort} onSort={onSort} />

      {open && (
        <Modal
          title={`${t("checkout.receipt")} №${open.order_number}`}
          onClose={() => setOpen(null)}
          footer={
            <>
              {Number(open.debt) > 0 && isAdmin && (
                <button onClick={() => setPaying(open)} disabled={busy}>
                  {t("receipts.acceptPayment")}
                </button>
              )}
              {isAdmin &&
                (open.payment_status === "PAID" || Number(open.amount_paid) > 0) &&
                !["REFUNDED", "PARTIALLY_REFUNDED"].includes(open.payment_status) &&
                open.status !== "CANCELLED" && (
                  <button className="secondary" onClick={(e) => undoPay(open, e)} disabled={busy}>
                    ↩ {t("receipts.unpay")}
                  </button>
                )}
              {canEdit && (
                <button className="secondary" onClick={() => setAdding(true)} disabled={busy}>
                  + {t("receipts.addBtn")}
                </button>
              )}
              {open.has_service && open.fulfillment_status === "PROCESSING" && (
                <button className="secondary" onClick={() => setFulfillment("READY")} disabled={busy}>
                  {t("receipts.markReady")}
                </button>
              )}
              {open.has_service && open.fulfillment_status === "READY" && (
                <button className="secondary" onClick={() => setFulfillment("ISSUED")} disabled={busy}>
                  {t("receipts.markIssued")}
                </button>
              )}
              {/* Откат прямо в окне чека: сюда заходят разбираться с заказом,
                  а промах по «Готово» замечают чаще всего именно здесь. */}
              {open.has_service && open.fulfillment_status !== "PROCESSING" && (
                <button
                  className="secondary"
                  onClick={() => setFulfillment("PROCESSING")}
                  disabled={busy}
                  title={t("receipts.rollbackTitle")}
                >
                  ← {t("receipts.markProcessing")}
                </button>
              )}
              {canRefund && (
                <button className="danger" onClick={() => setRefunding(true)} disabled={busy}>
                  {t("receipts.refund")}
                </button>
              )}
            </>
          }
        >
          {/* Строки с единицей и ценой, суммы целыми сомами — как в печатной
              форме и в списке чеков. */}
          {open.items.map((it) => {
            const unit = it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || "";
            return (
              <div className="crow" key={it.id}>
                <span>
                  {itemTitle(it, t)}
                  <span className="muted">
                    {" "}× {trimQty(it.quantity)} {unit} · {trimQty(it.price_per_item)}{" "}
                    {t("checkout.perPieceShort", { unit })}
                  </span>
                  {it.is_returned && (
                    <span className="badge warn" style={{ marginLeft: 6 }}>
                      {t("receipts.returned")}
                    </span>
                  )}
                </span>
                <span>{som(it.line_total)}</span>
              </div>
            );
          })}
          <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 8 }}>
            <strong>{t("common.total")}</strong>
            <strong>{som(open.total_price)}</strong>
          </div>
          {Number(open.amount_paid) > 0 && (
            <div className="crow">
              <span className="k">{t("receipts.paid")}</span>
              <span>{som(open.amount_paid)}</span>
            </div>
          )}
          {/* Часть заказа, закрытая сдачей с прошлых: без этой строки
              «оплачено 3 000» по заказу, за который принесли 2 000, выглядит
              как ошибка кассы. */}
          {Number(open.change_applied) > 0 && (
            <div className="crow">
              <span className="k">{t("checkout.changeUsed")}</span>
              <span>{som(open.change_applied)}</span>
            </div>
          )}
          {Number(open.change_due) > 0 && (
            <div className="crow">
              <span className="k">{t("receipts.change")}</span>
              <strong style={{ color: "var(--accent-strong)" }}>{som(open.change_due)}</strong>
            </div>
          )}
          {Number(open.debt) > 0 && (
            <div className="crow">
              <span className="k">{t("receipts.debt")}</span>
              <strong style={{ color: "var(--danger)" }}>{som(open.debt)}</strong>
            </div>
          )}
          <div className="crow">
            <span className="k">{t("receipts.status")}</span>
            <PaymentBadge status={open.payment_status} />
          </div>
          <div className="crow">
            <span className="k">{t("receipts.method")}</span>
            <span>{t(`checkout.${open.payment_method.toLowerCase()}`)}</span>
          </div>
          {open.cashier_name && (
            <div className="crow">
              <span className="k">{t("receipts.cashier")}</span>
              <span>
                {open.cashier_name}
                {open.cashier_role && <span className="muted"> · {open.cashier_role}</span>}
              </span>
            </div>
          )}
          {open.has_service && (
            <div className="crow">
              <span className="k">{t("receipts.fulfillment")}</span>
              <FulfillmentBadge status={open.fulfillment_status} />
            </div>
          )}
        </Modal>
      )}

      {adding && open && (
        <AddToOrderModal
          receiptId={open.id}
          onClose={() => setAdding(false)}
          onAdded={(data) => {
            setOpen(data);
            setAdding(false);
            load();
          }}
        />
      )}

      {refunding && open && (
        <RefundModal
          receipt={open}
          onClose={() => setRefunding(false)}
          onDone={(data) => {
            setOpen(data);
            setRefunding(false);
            load();
          }}
        />
      )}

      {printing && <PrintDocs receipt={printing} onClose={() => setPrinting(null)} />}

      {paying && (
        <PayDebtModal
          receipt={paying}
          onClose={() => setPaying(null)}
          onPaid={(data) => {
            setPaying(null);
            if (open && open.id === data.id) setOpen(data);
            load();
          }}
        />
      )}
    </>
  );
}
