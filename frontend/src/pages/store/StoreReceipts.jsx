import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import LoadError from "../../components/LoadError.jsx";
import Pager, { usePage } from "../../components/Pager.jsx";
import PayDebtModal from "../../components/PayDebtModal.jsx";
import PrintDocs from "../../components/PrintDocs.jsx";
import ReceiptCard from "../../components/ReceiptCard.jsx";
import { FulfillmentBadge, PaymentBadge, WarrantyBadge } from "../../components/StatusBadge.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { isCanceled, useLatest } from "../../utils/latest.js";
import { formatDateTime, formatMoney } from "../../utils/format.js";

const som = (n) => formatMoney(n);

export default function StoreReceipts() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const navigate = useNavigate();
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [listError, setListError] = useState(false);
  const [stats, setStats] = useState(null);
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(null);
  const [printing, setPrinting] = useState(null);
  const [advancingId, setAdvancingId] = useState(null);
  const { isAdmin, isAccountant } = useAuth();
  // Принять оплату долга может и складовщик (CLI-08): запись оплаты хранит, кто её
  // принял, и админ видит это в карточке. Откатить оплату — только админ.
  const canPay = !isAccountant;
  const [paying, setPaying] = useState(null);
  const [sort, setSort] = useState({ key: "_debt", dir: "desc" });
  const nextList = useLatest();
  const nextStats = useLatest();
  const filterKey = JSON.stringify([search, sort]);
  const [page, setPage] = usePage(filterKey);

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
  const PREV = { ISSUED: "READY", PARTIALLY_ISSUED: "READY", READY: "PROCESSING" };
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
  // Откат из «Выдан частично» снимает отметки «выдано» по всем позициям — это
  // история выдачи, поэтому спрашиваем.
  const rollback = async (r, e) => {
    e?.stopPropagation();
    if (r.fulfillment_status === "PARTIALLY_ISSUED" && !(await confirm(t("issue.rollbackAsk")))) return;
    return move(r, PREV[r.fulfillment_status], e);
  };

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
    api
      .get("/sales/receipts/", {
        params: { ...params, ordering: orderingParam(), ...(page > 1 ? { page } : {}) },
        signal: nextList(),
      })
      .then((r) => {
        setRows(r.data.results);
        setCount(r.data.count ?? r.data.results.length);
        setListError(false);
      })
      .catch((e) => {
        if (isCanceled(e)) return;
        if (e.response?.status === 404 && page > 1) return setPage(1);
        setListError(true);
        toast(apiError(e, t("common.loadFailed")), "error");
      });
    // Плитки — по тем же фильтрам, но не зависят от страницы.
    api
      .get("/sales/receipts/stats/", { params, signal: nextStats() })
      .then((r) => setStats(r.data))
      .catch((e) => {
        if (!isCanceled(e)) setStats(null);
      });
  }
  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, page]);

  const columns = [
    {
      key: "order_number",
      label: t("receipts.number"),
      sortKey: "order_number",
      render: (r) => (
        <>
          <strong>№{r.order_number ?? "—"}</strong>
          {r.is_warranty && (
            <div style={{ marginTop: 3 }}>
              <WarrantyBadge />
            </div>
          )}
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
        // Принять оплату может и складовщик, откатить её — только админ (бэкенд
        // на откат складовщику вернёт 403).
        const canUndo =
          isAdmin &&
          (r.payment_status === "PAID" || Number(r.amount_paid) > 0) &&
          !["REFUNDED", "PARTIALLY_REFUNDED"].includes(r.payment_status) &&
          r.status !== "CANCELLED";
        if (!hasDebt && !canUndo) return <span className="muted">0</span>;
        return (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            {hasDebt && <span style={{ color: "var(--danger-ink)", fontWeight: 600 }}>{som(r.debt)}</span>}
            {hasDebt && canPay && (
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
      render: (r) => formatDateTime(r.created_at),
    },
    {
      key: "actions",
      label: t("common.actions"),
      render: (r) => (
        <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
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
          <button className="ghost" onClick={(e) => { e.stopPropagation(); setOpen(r); }} aria-label={`${t("common.edit")} №${r.order_number}`}>
            <Icon name="arrow-right" size={18} />
          </button>
        </div>
      ),
    },
  ];

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
            <div className="value" style={Number(stats.debt) > 0 ? { color: "var(--danger-ink)" } : undefined}>
              {formatMoney(stats.debt)}
            </div>
          </div>
        </div>
      )}
      <div className="toolbar">
        <input
          className="search"
          type="search"
          aria-label={t("receiptsV2.searchPh")}
          placeholder={t("receiptsV2.searchPh")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      {listError && !rows.length ? (
        <LoadError onRetry={load} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          sort={sort}
          onSort={onSort}
          onRowClick={setOpen}
          filtered={!!search}
          onReset={() => setSearch("")}
        />
      )}
      <Pager page={page} count={count} onPage={setPage} />

      {/* Карточка заказа общая с админским экраном «Чеки»: оплата, выдача по
          позициям, дозаказ, возврат, печать и наряд мастеру — по роли. */}
      {open && (
        <ReceiptCard
          receipt={open}
          onClose={() => setOpen(null)}
          onChange={(data) => {
            setOpen(data);
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
