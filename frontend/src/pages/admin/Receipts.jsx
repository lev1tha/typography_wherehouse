import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import api from "../../api/api.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import AddToOrderModal from "../../components/AddToOrderModal.jsx";
import DataTable from "../../components/DataTable.jsx";
import EditReceiptModal from "../../components/EditReceiptModal.jsx";
import RefundModal from "../../components/RefundModal.jsx";
import GiveChangeModal from "../../components/GiveChangeModal.jsx";
import Icon from "../../components/Icon.jsx";
import PayDebtModal from "../../components/PayDebtModal.jsx";
import PrintDocs from "../../components/PrintDocs.jsx";
import { FulfillmentBadge, PaymentBadge } from "../../components/StatusBadge.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;

function ReceiptsTab() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const navigate = useNavigate();
  const { isAdmin, isAccountant, seesMoney } = useAuth();
  // Бухгалтер только смотрит: сервер его записи не примет, и показывать кнопки,
  // которые гарантированно ответят 403, — это обещать то, чего нет.
  const readOnly = isAccountant;
  const [rows, setRows] = useState([]);
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
  const [clientsList, setClientsList] = useState([]);
  const [advancingId, setAdvancingId] = useState(null);
  const [paying, setPaying] = useState(null);
  const [givingChange, setGivingChange] = useState(null);
  const [editing, setEditing] = useState(null);
  const [adding, setAdding] = useState(null);
  const [refunding, setRefunding] = useState(null);
  // Заказ, по которому открыты печатные формы (чек / накладная / счёт).
  const [printing, setPrinting] = useState(null);
  const [sort, setSort] = useState({ key: "_debt", dir: "desc" });

  const filtered = method || pstatus || search || client || dateFrom || dateTo || onlyChange;

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

  function load() {
    const params = {};
    if (method) params.payment_method = method;
    if (pstatus) params.payment_status = pstatus;
    if (search) params.search = search;
    if (client) params.client = client;
    if (dateFrom) params.date_from = dateFrom;
    if (dateTo) params.date_to = dateTo;
    if (onlyChange) params.has_change = "1";
    api.get("/sales/receipts/", { params: { ...params, ordering: orderingParam() } }).then((r) => setRows(r.data.results));
    // Плитки сверху считаются по ТЕМ ЖЕ фильтрам: иначе «Долг» показывал бы
    // общий долг цеха под отфильтрованным списком одного клиента.
    api.get("/sales/receipts/stats/", { params }).then((r) => setStats(r.data));
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
  }, [method, pstatus, search, client, dateFrom, dateTo, onlyChange, sort]);

  useEffect(() => {
    api.get("/clients/clients/").then((r) => setClientsList(r.data.results)).catch(() => {});
  }, []);

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
      render: (r) => (
        <>
          <strong>№{r.order_number ?? "—"}</strong>
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
    { key: "total_price", label: t("common.total"), sortKey: "total_price", render: (r) => <strong>{som(r.total_price)}</strong> },
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
                  <strong style={{ color: Number(r.margin) < 0 ? "var(--danger)" : undefined }}>
                    {som(r.margin)}
                  </strong>
                )}
                {/* Ноль себестоимости значит «материала в заказе не было»
                    (чистая услуга) либо старый заказ до учёта себестоимости. */}
                <div className="muted" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
                  {t("receipts.costShort")}: {Number(r.cost_total) > 0 ? som(r.cost_total) : "—"}
                </div>
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
            {hasDebt && <span style={{ color: "var(--danger)", fontWeight: 600, whiteSpace: "nowrap" }}>{som(r.debt)}</span>}
            {hasDebt && !readOnly && (
              <button
                className="secondary row-btn"
                onClick={(e) => { e.stopPropagation(); setPaying(r); }}
              >
                {t("receipts.pay")}
              </button>
            )}
            {due > 0 && (
              <span style={{ color: "var(--accent-strong)", fontWeight: 600, whiteSpace: "nowrap" }}>
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
            {d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" })}
            <div className="muted" style={{ fontSize: 12 }}>
              {d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}
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
          <div className="stat">
            <div className="label">{t("receipts.debt")}</div>
            <div className="value" style={Number(stats.debt) > 0 ? { color: "var(--danger)" } : undefined}>
              {som(stats.debt)}
            </div>
          </div>
          {/* Сдача — сколько цех должен клиентам. Стоит рядом с долгом: это две
              стороны одного вопроса «кто кому остался должен». */}
          <div className="stat">
            <div className="label">{t("receipts.statChange")}</div>
            <div
              className="value"
              style={Number(stats.change_due) > 0 ? { color: "var(--accent-strong)" } : undefined}
            >
              {som(stats.change_due)}
            </div>
          </div>
        </div>
      )}
      <div className="toolbar">
        <input
          className="search"
          placeholder={t("common.search")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={method} onChange={(e) => setMethod(e.target.value)}>
          <option value="">{t("receipts.method")}: {t("common.all")}</option>
          <option value="CASH">{t("checkout.cash")}</option>
          <option value="MBANK">{t("checkout.mbank")}</option>
          <option value="DEMIRBANK">{t("checkout.demirbank")}</option>
          <option value="ONLINE">{t("checkout.online")}</option>
        </select>
        <select value={pstatus} onChange={(e) => setPstatus(e.target.value)}>
          <option value="">{t("receipts.status")}: {t("common.all")}</option>
          {["PENDING", "PAID", "REFUNDED", "PARTIALLY_REFUNDED"].map((s) => (
            <option key={s} value={s}>
              {t(`payment.${s}`)}
            </option>
          ))}
        </select>
        <select value={client} onChange={(e) => setClient(e.target.value)}>
          <option value="">{t("checkout.client")}: {t("common.all")}</option>
          {clientsList.map((c) => (
            <option key={c.id} value={c.id}>
              {c.display_name}
            </option>
          ))}
        </select>
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
          onClick={() => setOnlyChange((v) => !v)}
        >
          {t("receipts.onlyChange")}
        </button>
        {filtered && (
          <button className="ghost" onClick={resetFilters}>
            {t("common.reset")}
          </button>
        )}
      </div>
      {/* С себестоимостью и маржой колонок стало одиннадцать — таблица
          прокручивается вбок сама, а не тянет за собой всю страницу. */}
      <div className="table-wrap dense">
        <DataTable columns={columns} rows={rows} sort={sort} onSort={onSort} />
      </div>

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

function auditIcon(action = "") {
  const a = action.toLowerCase();
  if (a.includes("вход")) return "key";
  if (a.includes("возврат")) return "undo";
  if (a.includes("цен")) return "tag";
  if (a.includes("оформлен чек") || a.includes("чек")) return "receipt";
  if (a.includes("инвентар")) return "clipboard";
  if (a.includes("списан")) return "trash";
  if (a.includes("поступлен")) return "inbox";
  if (a.includes("готов") || a.includes("выдан")) return "check-circle";
  if (a.includes("дозаказ")) return "plus-circle";
  if (a.includes("реферер")) return "shuffle";
  return "dot";
}

function AuditTab() {
  const { t } = useTranslation();
  const [rows, setRows] = useState([]);
  useEffect(() => {
    api.get("/audit/logs/").then((r) => setRows(r.data.results));
  }, []);

  if (!rows.length) {
    return (
      <div className="empty-state">
        <Icon name="archive" size={40} className="es-icon" />
        {t("common.empty")}
      </div>
    );
  }

  return (
    <div className="feed">
      {rows.map((r) => (
        <div className="feed-item" key={r.id}>
          <div className="feed-icon"><Icon name={auditIcon(r.action)} size={17} /></div>
          <div className="feed-body">
            <div className="feed-action">{r.action}</div>
            <div className="feed-meta">
              {r.username || "—"} · {new Date(r.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function Receipts() {
  const { t } = useTranslation();
  const [tab, setTab] = useState("receipts");

  return (
    <>
      <h1>{t("receipts.title")}</h1>
      <div className="tabs">
        <button
          className={tab === "receipts" ? "active" : ""}
          onClick={() => setTab("receipts")}
        >
          {t("receipts.title")}
        </button>
        <button className={tab === "audit" ? "active" : ""} onClick={() => setTab("audit")}>
          {t("nav.audit")}
        </button>
      </div>
      {tab === "receipts" ? <ReceiptsTab /> : <AuditTab />}
    </>
  );
}
