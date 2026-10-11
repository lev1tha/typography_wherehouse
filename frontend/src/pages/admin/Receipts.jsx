import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import AddToOrderModal from "../../components/AddToOrderModal.jsx";
import AuditJournal from "../../components/AuditJournal.jsx";
import ClientPicker from "../../components/ClientPicker.jsx";
import DataTable from "../../components/DataTable.jsx";
import EditReceiptModal from "../../components/EditReceiptModal.jsx";
import RefundModal from "../../components/RefundModal.jsx";
import GiveChangeModal from "../../components/GiveChangeModal.jsx";
import Icon from "../../components/Icon.jsx";
import LoadError from "../../components/LoadError.jsx";
import Pager, { usePage } from "../../components/Pager.jsx";
import PayDebtModal from "../../components/PayDebtModal.jsx";
import PrintDocs from "../../components/PrintDocs.jsx";
import ReceiptCard from "../../components/ReceiptCard.jsx";
import { FulfillmentBadge, PaymentBadge, WarrantyBadge } from "../../components/StatusBadge.jsx";
import Tabs, { tabPanel } from "../../components/Tabs.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { isCanceled, useLatest } from "../../utils/latest.js";
import { receiptRuled, rulesLabel } from "../../utils/pricingRules.js";
import { formatDate, formatDateTime, formatMoney, formatTime } from "../../utils/format.js";
import { downloadFile } from "../../utils/download.js";

const som = (n) => formatMoney(n);

function ReceiptsTab() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const navigate = useNavigate();
  const { isAdmin, isAccountant, seesMoney } = useAuth();
  // Бухгалтер только смотрит: сервер его записи не примет, и показывать кнопки,
  // которые гарантированно ответят 403, — это обещать то, чего нет.
  const readOnly = isAccountant;
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [listError, setListError] = useState(false);
  const [stats, setStats] = useState(null);
  const [method, setMethod] = useState("");
  const [pstatus, setPstatus] = useState("");
  const [search, setSearch] = useState("");
  // Фильтр по клиенту и по датам ЕГО заказов: «покажи всё, что Тахир заказывал
  // в июле» — через поиск по строке это не спрашивается, поиск ищет одно слово.
  const [client, setClient] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  // «Кому мы должны отдать сдачу» — рабочий список кассира.
  const [onlyChange, setOnlyChange] = useState(false);
  // На телефоне фильтры свёрнуты под кнопку «Фильтры» (RU-N26): до первого
  // чека было десять блоков. На широком экране кнопки нет, фильтры видны.
  const [filtersOpen, setFiltersOpen] = useState(false);
  const activeFilters = [method, pstatus, client, dateFrom, dateTo, onlyChange].filter(Boolean).length;
  const [advancingId, setAdvancingId] = useState(null);
  const [paying, setPaying] = useState(null);
  const [givingChange, setGivingChange] = useState(null);
  const [editing, setEditing] = useState(null);
  const [adding, setAdding] = useState(null);
  const [refunding, setRefunding] = useState(null);
  // Открытая карточка заказа: состав с размерами деталей, оплаты, выдача по
  // позициям, списание долга. Щелчок по строке таблицы или кнопка «Открыть».
  const [open, setOpen] = useState(null);
  // Заказ, по которому открыты печатные формы (чек / накладная / счёт).
  const [printing, setPrinting] = useState(null);
  const [sort, setSort] = useState({ key: "_debt", dir: "desc" });

  const filtered = method || pstatus || search || client || dateFrom || dateTo || onlyChange;
  // Поиск и фильтры шлют запрос на каждое нажатие — побеждает последний; смена
  // фильтра или сортировки возвращает на первую страницу.
  const nextList = useLatest();
  const nextStats = useLatest();
  const filterKey = JSON.stringify([method, pstatus, search, client, dateFrom, dateTo, onlyChange, sort]);
  const [page, setPage] = usePage(filterKey);

  function resetFilters() {
    setMethod(""); setPstatus(""); setSearch("");
    setClient(""); setDateFrom(""); setDateTo(""); setOnlyChange(false);
  }

  function orderingParam() {
    // Вторичная сортировка по дате (кроме случая, когда уже сортируем по дате).
    const tail = sort.key !== "created_at" ? ",-created_at" : "";
    return (sort.dir === "desc" ? "-" : "") + sort.key + tail;
  }

  function onSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" }));
  }

  // Фильтры списка — одни для страницы, плиток и выгрузки CSV.
  function listParams() {
    const params = {};
    if (method) params.payment_method = method;
    if (pstatus) params.payment_status = pstatus;
    if (search) params.search = search;
    if (client) params.client = client;
    if (dateFrom) params.date_from = dateFrom;
    if (dateTo) params.date_to = dateTo;
    if (onlyChange) params.has_change = "1";
    return params;
  }

  // CSV «Чеки со строками» (волна 2): те же фильтры и сортировка, все страницы.
  function exportCsv() {
    downloadFile("/sales/receipts/export/", { ...listParams(), ordering: orderingParam() }, "cheki.csv").catch((e) =>
      toast(apiError(e, t("common.error")), "error")
    );
  }

  function load() {
    const params = listParams();
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
    // Плитки сверху считаются по ТЕМ ЖЕ фильтрам (и не зависят от страницы):
    // иначе «Долг» показывал бы общий долг цеха под отфильтрованным списком
    // одного клиента, а «Всего» — число строк на странице.
    api
      .get("/sales/receipts/stats/", { params, signal: nextStats() })
      .then((r) => setStats(r.data))
      .catch((e) => {
        if (!isCanceled(e)) setStats(null);
      });
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

  // Удаление — не возврат: возврат клиент принёс обратно, и в отчётах он обязан
  // остаться; удаление — это «такого заказа не было». Поэтому и текст
  // подтверждения перечисляет последствия, а не спрашивает «уверены?».
  // Возвращать есть что, пока чек не отменён и не возвращён целиком.
  const canRefund = (r) =>
    !["REFUNDED", "CANCELLED"].includes(r.payment_status) &&
    r.status !== "CANCELLED" &&
    (r.items || []).some((i) => !i.is_returned);
  // Дозаказ — то же правило, что у складовщика: нельзя в возвращённый и
  // отменённый. Выданный отклоняет сервер («оформите новый»), и это правильное
  // место: правило одно, а экранов с дозаказом теперь два.
  const canAdd = (r) => r.payment_status !== "REFUNDED" && r.status !== "CANCELLED";

  async function removeReceipt(r, e) {
    e?.stopPropagation();
    const ok = await confirm(
      t("receipts.deleteConfirm", { number: r.order_number, total: som(r.total_price) }),
    );
    if (!ok) return;
    try {
      await api.delete(`/sales/receipts/${r.id}/`);
      load();
      toast(t("receipts.deleted"));
    } catch (err) {
      toast(err.response?.data?.detail || t("common.error"), "error");
    }
  }

  async function undoPay(r, e) {
    e?.stopPropagation();
    if (!(await confirm(t("receipts.confirmUnpay")))) return;
    try {
      await api.post(`/sales/receipts/${r.id}/unpay/`, {});
      load();
      toast(t("receipts.unpayDone"));
    } catch (err) {
      toast(err.response?.data?.detail || t("common.error"), "error");
    }
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, page]);

  // Таблица уложена в десять колонок, чтобы влезать в обычный монитор:
  // раньше их было четырнадцать (1650px при 1440 у заказчика), и действия по
  // чеку — печать, правка, возврат — жили за правым краем, куда никто не
  // крутил. Что переехало: «кто оформил» — под клиента, способ оплаты — под
  // статус, себестоимость — под маржу, сдача — в колонку долга (они не бывают
  // одновременно), печать — к остальным действиям.
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
      key: "client_name",
      label: t("checkout.client"),
      render: (r) => (
        <>
          {r.client_name || "—"}
          {r.cashier_name && (
            <div className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
              {t("receipts.cashierShort")}: {r.cashier_name}
            </div>
          )}
        </>
      ),
    },
    {
      key: "payment_status",
      label: t("receipts.status"),
      render: (r) => (
        <>
          <PaymentBadge status={r.payment_status} />
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            {t(`checkout.${r.payment_method.toLowerCase()}`)}
          </div>
        </>
      ),
    },
    {
      key: "fulfillment",
      label: t("receipts.fulfillment"),
      render: (r) =>
        r.has_service ? (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            <FulfillmentBadge status={r.fulfillment_status} />
            {PREV[r.fulfillment_status] && !readOnly && (
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
            {r.fulfillment_status !== "ISSUED" && !readOnly && (
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
    // Итог — тем же форматом, что остальные суммы («1 879 сом», не «1879.00»).
    {
      key: "total_price",
      label: t("common.total"),
      sortKey: "total_price",
      render: (r) => (
        <>
          <strong>{som(r.total_price)}</strong>
          {/* Правила прайса: «по каталогу» и что сработало — срочность, скидка. */}
          {receiptRuled(r) && (
            <div className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
              <s>{som(r.catalog_total)}</s> · {rulesLabel(r, t)}
            </div>
          )}
        </>
      ),
    },
    // Маржа и под ней себестоимость проданного по заказу. Снимок закупки на
    // момент продажи — переоценка склада прошлые заказы не двигает. Видят
    // владелец и бухгалтер: складовщик оформляет и выдаёт, но закупочных цен
    // не знает.
    ...(seesMoney
      ? [
          {
            key: "margin",
            label: t("receipts.margin"),
            render: (r) => (
              <>
                {r.margin == null ? (
                  <span className="muted">—</span>
                ) : (
                  <strong style={{ color: Number(r.margin) < 0 ? "var(--danger-ink)" : undefined }}>
                    {som(r.margin)}
                  </strong>
                )}
                {/* Ноль себестоимости значит «материала в заказе не было»
                    (чистая услуга) либо старый заказ до учёта себестоимости. */}
                <div className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                  {t("receipts.costShort")}: {Number(r.cost_total) > 0 ? som(r.cost_total) : "—"}
                </div>
                {/* Гарантийные переделки исходного заказа: материал на них
                    списан, а выручки с них нет — честная маржа ниже. */}
                {Number(r.warranty_cost) > 0 && r.margin_net != null && (
                  <div className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                    {t("warranty.afterShort")}: <strong>{som(r.margin_net)}</strong>
                  </div>
                )}
              </>
            ),
          },
        ]
      : []),
    // Долг клиента и сдача цеха перед ним — две стороны одного вопроса «кто
    // кому остался должен», одновременно не бывают, поэтому делят колонку.
    {
      key: "debt",
      label: `${t("receipts.debt")} / ${t("receipts.change")}`,
      sortKey: "_debt",
      render: (r) => {
        const hasDebt = Number(r.debt) > 0;
        const due = Math.round(Number(r.change_due) || 0);
        const canUndo =
          !readOnly &&
          (r.payment_status === "PAID" || Number(r.amount_paid) > 0) &&
          !["REFUNDED", "PARTIALLY_REFUNDED"].includes(r.payment_status) &&
          r.status !== "CANCELLED";
        // Единственное место, где сумма выводилась без «сом». В денежной
        // колонке голый «0» читается как незаполненное поле, а не как «долга нет».
        if (!hasDebt && due <= 0 && !canUndo) return <span className="muted">{som(0)}</span>;
        return (
          <div className="row" style={{ gap: 6, alignItems: "center", margin: 0 }}>
            {hasDebt && <span style={{ color: "var(--danger-ink)", fontWeight: 600, whiteSpace: "nowrap" }}>{som(r.debt)}</span>}
            {hasDebt && !readOnly && (
              <button
                className="secondary row-btn"
                onClick={(e) => { e.stopPropagation(); setPaying(r); }}
              >
                {t("receipts.pay")}
              </button>
            )}
            {due > 0 && (
              <span style={{ color: "var(--accent-ink)", fontWeight: 600, whiteSpace: "nowrap" }}>
                {t("receipts.change")}: {som(due)}
              </span>
            )}
            {due > 0 && !readOnly && isAdmin && (
              <button
                className="secondary row-btn"
                onClick={(e) => { e.stopPropagation(); setGivingChange(r); }}
              >
                {t("receipts.changeGive")}
              </button>
            )}
            {canUndo && (
              <button
                className="ghost row-btn"
                style={{ color: "var(--ink-muted)" }}
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
      render: (r) => {
        const d = new Date(r.created_at);
        return (
          <span style={{ whiteSpace: "nowrap" }}>
            {formatDate(d)}
            <div className="muted" style={{ fontSize: 12 }}>
              {formatTime(d)}
            </div>
          </span>
        );
      },
    },
    // Действия одной ячейкой, кнопки ПОДПИСАНЫ, а не одни иконки: на складе те
    // же иконки без подписей заказчик просто не нашёл и решил, что функции нет.
    // Печать — всем ролям (накладную выдаёт складовщик, счёт спрашивает
    // бухгалтерия клиента), правка, возврат и удаление — админу.
    {
      key: "actions",
      label: isAdmin ? t("receipts.actions") : "",
      render: (r) => (
        <div className="row-actions">
          {/* Карточка заказа. Щелчок по строке открывает её мышью; кнопка — для
              клавиатуры и для экранного диктора (сама строка не фокусируется). */}
          <button
            className="ghost row-btn"
            onClick={(e) => { e.stopPropagation(); setOpen(r); }}
            aria-label={t("receiptsV2.openCard", { number: r.order_number })}
            title={t("receiptsV2.openCard", { number: r.order_number })}
          >
            <Icon name="arrow-right" size={14} /> {t("receiptsV2.open")}
          </button>
          {/* «Повторить» — половина заказов у типографии повторные: те же
              визитки, та же вывеска. Состав переносится в кассу, цены берутся
              сегодняшние; кассиру остаётся нажать «Оформить». */}
          {!readOnly && (
            <button
              className="secondary row-btn"
              onClick={(e) => { e.stopPropagation(); navigate(`/admin?repeat=${r.id}`); }}
              title={t("receipts.repeatHint")}
            >
              <Icon name="undo" size={14} /> {t("receipts.repeat")}
            </button>
          )}
          <button
            className="secondary row-btn"
            onClick={(e) => { e.stopPropagation(); setPrinting(r); }}
            title={t("print.title")}
          >
            <Icon name="printer" size={14} /> {t("print.print")}
          </button>
          {isAdmin && (
            <>
              <button
                className="secondary row-btn"
                onClick={(e) => { e.stopPropagation(); setEditing(r); }}
              >
                <Icon name="pencil" size={14} /> {t("receipts.edit")}
              </button>
              {/* Дозаказ. У складовщика он был с самого начала, у админа —
                  нет: «клиент попросил ещё одну деталь к тому же заказу»
                  админу приходилось делать чужими руками или новым чеком. */}
              {canAdd(r) && (
                <button
                  className="secondary row-btn"
                  onClick={(e) => { e.stopPropagation(); setAdding(r); }}
                >
                  <Icon name="plus" size={14} /> {t("receipts.addBtn")}
                </button>
              )}
              {/* Возврат — целиком или отдельными позициями. Раньше у админа
                  этой кнопки не было вовсе: возврат жил только в складском
                  разделе, и только целым чеком. */}
              {canRefund(r) && (
                <button
                  className="secondary row-btn"
                  onClick={(e) => { e.stopPropagation(); setRefunding(r); }}
                >
                  <Icon name="undo" size={14} /> {t("receipts.refundBtn")}
                </button>
              )}
              <button
                className="ghost row-btn row-danger"
                onClick={(e) => removeReceipt(r, e)}
              >
                <Icon name="trash" size={14} /> {t("receipts.delete")}
              </button>
            </>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      {stats && (
        <div className="stat-grid" style={{ marginBottom: 16 }}>
          <div className="stat"><div className="label">{t("receipts.statTotal")}</div><div className="value">{stats.total}</div></div>
          <div className="stat"><div className="label">{t("receipts.statWorking")}</div><div className="value">{stats.working}</div></div>
          <div className="stat"><div className="label">{t("receipts.statReady")}</div><div className="value">{stats.ready}</div></div>
          {/* Долг по ЗАКАЗАМ (с фильтрами списка): входящие долги до переезда
              сюда не входят, весь долг клиентов — в «Клиентах» (RU-N5). */}
          <div className="stat">
            <div className="label">{t("receipts.debtOrders")}</div>
            <div className="value" style={Number(stats.debt) > 0 ? { color: "var(--danger-ink)" } : undefined}>
              {som(stats.debt)}
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {t("receipts.debtOrdersNote")}
              <button
                type="button" className="ghost" style={{ paddingLeft: 0, height: "auto", fontSize: 12, display: "block" }}
                onClick={() => navigate("/admin/clients")}
              >
                {t("receipts.debtAllInClients")}
              </button>
            </div>
          </div>
          {/* Сдача — сколько цех должен клиентам. Стоит рядом с долгом: это две
              стороны одного вопроса «кто кому остался должен». */}
          <div className="stat">
            <div className="label">{t("receipts.statChange")}</div>
            <div
              className="value"
              style={Number(stats.change_due) > 0 ? { color: "var(--accent-ink)" } : undefined}
            >
              {som(stats.change_due)}
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
        <button
          type="button"
          className="secondary filters-toggle"
          aria-expanded={filtersOpen}
          aria-controls="receipts-filters"
          onClick={() => setFiltersOpen((v) => !v)}
        >
          {t("receipts.filtersBtn")}{activeFilters ? ` · ${activeFilters}` : ""}
        </button>
        <div id="receipts-filters" className={`filters-more${filtersOpen ? " open" : ""}`}>
        <select aria-label={t("receipts.method")} value={method} onChange={(e) => setMethod(e.target.value)}>
          <option value="">{t("receipts.method")}: {t("common.all")}</option>
          <option value="CASH">{t("checkout.cash")}</option>
          <option value="MBANK">{t("checkout.mbank")}</option>
          <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
          <option value="ONLINE">{t("checkout.online")}</option>
        </select>
        <select aria-label={t("receipts.status")} value={pstatus} onChange={(e) => setPstatus(e.target.value)}>
          <option value="">{t("receipts.status")}: {t("common.all")}</option>
          {["PENDING", "PAID", "REFUNDED", "PARTIALLY_REFUNDED"].map((s) => (
            <option key={s} value={s}>
              {t(`payment.${s}`)}
            </option>
          ))}
        </select>
        {/* Клиент — поиском по серверу: раньше это был <select> с первыми 25
            клиентами, и остальных в фильтре просто не было. */}
        <ClientPicker
          value={client}
          noneLabel={`${t("checkout.client")}: ${t("common.all")}`}
          onChange={(id) => setClient(id === "" ? "" : String(id))}
          aria-label={t("checkout.client")}
        />
        {/* Даты подписаны прямо в поле: без подписи два одинаковых календаря
            рядом не читаются — непонятно, где «с», а где «по». */}
        {/* Оба календаря — одной неразрывной парой: поодиночке «С» оставалось в
            первой строке, а «По» падало во вторую. */}
        <div className="row" style={{ gap: 8, margin: 0, flexWrap: "nowrap" }}>
          <label className="filter-date">
            <span>{t("dashboard.from")}</span>
            <input type="date" value={dateFrom} max={dateTo || undefined} onChange={(e) => setDateFrom(e.target.value)} />
          </label>
          <label className="filter-date">
            <span>{t("dashboard.to")}</span>
            <input type="date" value={dateTo} min={dateFrom || undefined} onChange={(e) => setDateTo(e.target.value)} />
          </label>
        </div>
        <button
          className={onlyChange ? "" : "secondary"}
          aria-pressed={onlyChange}
          onClick={() => setOnlyChange((v) => !v)}
        >
          {t("receipts.onlyChange")}
        </button>
        {filtered && (
          <button className="ghost" onClick={resetFilters}>
            {t("common.reset")}
          </button>
        )}
        <button className="secondary" onClick={exportCsv}>{t("clients.exportCsv")}</button>
        </div>
      </div>
      {/* С себестоимостью и маржой колонок стало одиннадцать — таблица
          прокручивается вбок сама, а не тянет за собой всю страницу. */}
      <div className="table-wrap dense">
        {listError && !rows.length ? (
          <LoadError onRetry={load} />
        ) : (
          <DataTable
            columns={columns}
            rows={rows}
            sort={sort}
            onSort={onSort}
            onRowClick={setOpen}
            filtered={!!filtered}
            onReset={resetFilters}
          />
        )}
      </div>
      <Pager page={page} count={count} onPage={setPage} />

      {paying && (
        <PayDebtModal
          receipt={paying}
          onClose={() => setPaying(null)}
          onPaid={() => { setPaying(null); load(); }}
        />
      )}

      {givingChange && (
        <GiveChangeModal
          receipt={givingChange}
          onClose={() => setGivingChange(null)}
          onGiven={() => { setGivingChange(null); load(); }}
        />
      )}

      {printing && <PrintDocs receipt={printing} onClose={() => setPrinting(null)} />}

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

      {editing && (
        <EditReceiptModal
          receipt={editing}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }}
        />
      )}

      {adding && (
        <AddToOrderModal
          receiptId={adding.id}
          receipt={adding}
          onClose={() => setAdding(null)}
          onAdded={() => { setAdding(null); load(); }}
        />
      )}

      {refunding && (
        <RefundModal
          receipt={refunding}
          onClose={() => setRefunding(null)}
          onDone={() => { setRefunding(null); load(); }}
        />
      )}
    </>
  );
}

export default function Receipts() {
  const { t } = useTranslation();
  const [tab, setTab] = useState("receipts");

  return (
    <>
      <h1>{t("receipts.title")}</h1>
      <Tabs
        id="receipts"
        label={t("receipts.title")}
        value={tab}
        onChange={setTab}
        tabs={[
          { key: "receipts", label: t("receipts.title") },
          { key: "audit", label: t("nav.audit") },
        ]}
      />
      <div {...tabPanel("receipts", tab)}>
        {tab === "receipts" ? <ReceiptsTab /> : <AuditJournal />}
      </div>
    </>
  );
}
