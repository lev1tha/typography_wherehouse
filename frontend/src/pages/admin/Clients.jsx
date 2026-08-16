import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import BulkPayModal from "../../components/BulkPayModal.jsx";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import PrintAct from "../../components/PrintAct.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";

// Сколько заказов показывать в карточке сразу — остальные под кнопкой.
const ORDERS_PREVIEW = 5;
// Оплат в карточке показываем столько же: история длиннее — в чеках.
const PAYMENTS_PREVIEW = 5;

export default function Clients() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin } = useAuth();
  const [clients, setClients] = useState([]);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState(null);
  // Клиент, по которому открыт акт сверки.
  const [actFor, setActFor] = useState(null);
  const [reqForm, setReqForm] = useState({ referred_by: "", reason: "" });
  const [issuedPassword, setIssuedPassword] = useState(null); // показывается один раз
  const [period, setPeriod] = useState({ year: new Date().getFullYear(), month: null });
  const [day, setDay] = useState(""); // конкретный день внутри месяца
  const [sort, setSort] = useState({ key: "sort_name", dir: "asc" });
  const [showAllOrders, setShowAllOrders] = useState(false);
  const [showAllPayments, setShowAllPayments] = useState(false);
  const [onlyDebt, setOnlyDebt] = useState(false);
  const [minOrders, setMinOrders] = useState("");
  // Клиента можно завести заранее, не дожидаясь продажи.
  const [creating, setCreating] = useState(null);
  // Общая выплата — одна сумма сразу за несколько заказов.
  const [payingClient, setPayingClient] = useState(null);
  // Склейка двойников: {from, preview} — что именно переедет, показываем до
  // подтверждения, потому что вторая карточка удаляется безвозвратно.
  const [merging, setMerging] = useState(null);
  // «Кому мы должны сдачу» — обратный список к должникам.
  const [onlyChange, setOnlyChange] = useState(false);

  // День важнее месяца: выбран день — смотрим ровно его, иначе весь месяц.
  function periodParams() {
    if (day) return { date_from: day, date_to: day };
    if (!period.month) return {};
    const last = new Date(period.year, period.month, 0).getDate();
    const mm = String(period.month).padStart(2, "0");
    return {
      date_from: `${period.year}-${mm}-01`,
      date_to: `${period.year}-${mm}-${String(last).padStart(2, "0")}`,
    };
  }

  function load() {
    const params = {
      ...(search ? { search } : {}),
      ...periodParams(),
      ...(onlyDebt ? { has_debt: 1 } : {}),
      ...(onlyChange ? { has_change: 1 } : {}),
      ...(Number(minOrders) > 0 ? { min_orders: Number(minOrders) } : {}),
      ordering: (sort.dir === "desc" ? "-" : "") + sort.key,
    };
    api.get("/clients/clients/", { params }).then((r) => setClients(r.data.results));
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, period.year, period.month, day, onlyDebt, onlyChange, minOrders, sort.key, sort.dir]);

  function onSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));
  }

  async function openDetail(c) {
    const { data } = await api.get(`/clients/clients/${c.id}/`, { params: periodParams() });
    setDetail(data);
    setShowAllOrders(false);
    setShowAllPayments(false);
    setMerging(null);
    setReqForm({ referred_by: "", reason: "" });
  }

  // После выплаты перечитываем карточку и список: изменились и долги заказов,
  // и колонка «Долг» в таблице.
  async function refreshDetail(id) {
    const { data } = await api.get(`/clients/clients/${id}/`, { params: periodParams() });
    setDetail(data);
    load();
  }

  // ИНН юрлица — нужен счёту на оплату. Сохраняем по уходу из поля, как
  // остальные правки в карточке.
  async function saveInn(value) {
    const next = value.trim();
    if (next === (detail.inn || "")) return;
    try {
      await api.patch(`/clients/clients/${detail.id}/`, { inn: next });
      await refreshDetail(detail.id);
      toast(t("common.saved"));
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    }
  }

  // Пароль кабинета выдаёт админ и диктует клиенту. Показывается один раз:
  // в базе только хеш, посмотреть повторно нельзя — можно выдать новый.
  async function issuePassword() {
    if (detail.has_password && !(await confirm(t("clients.reissuePassConfirm")))) return;
    try {
      const { data } = await api.post(`/clients/clients/${detail.id}/set-password/`, {});
      setIssuedPassword(data.password);
      const fresh = await api.get(`/clients/clients/${detail.id}/`);
      setDetail(fresh.data);
    } catch {
      toast(t("common.error"), "error");
    }
  }

  const errMsg = (e) => apiError(e, t("common.error"));

  async function createClient() {
    const body = { ...creating };
    if (!body.phone?.trim()) return toast(t("clients.needPhone"), "error");
    try {
      await api.post("/clients/clients/", body);
      setCreating(null);
      load();
      toast(t("clients.created"));
    } catch (e) {
      toast(errMsg(e), "error");
    }
  }

  // Смотрим, что переедет, ДО подтверждения: склейка удаляет вторую карточку
  // и откатить её нечем.
  async function previewMerge(fromId) {
    if (!fromId) return setMerging(null);
    try {
      const { data } = await api.get(`/clients/clients/${detail.id}/merge-preview/`, {
        params: { from: fromId },
      });
      setMerging({ from: fromId, preview: data });
    } catch (e) {
      toast(errMsg(e), "error");
      setMerging(null);
    }
  }

  async function doMerge() {
    if (!merging) return;
    const { preview } = merging;
    const question = t("clients.mergeConfirm", {
      drop: preview.drop,
      keep: preview.keep,
      orders: preview.orders,
    });
    if (!(await confirm(question))) return;
    try {
      await api.post(`/clients/clients/${detail.id}/merge/`, { from: merging.from });
      setMerging(null);
      await refreshDetail(detail.id);
      toast(t("clients.mergeDone", { name: preview.drop }));
    } catch (e) {
      toast(errMsg(e), "error");
    }
  }

  async function setReferrer(value) {
    try {
      const { data } = await api.patch(`/clients/clients/${detail.id}/`, { referred_by: value || null });
      // re-fetch detail (full referral data) and refresh list
      const fresh = await api.get(`/clients/clients/${data.id}/`);
      setDetail(fresh.data);
      load();
      toast(t("common.save"));
    } catch (e) {
      toast(errMsg(e), "error");
    }
  }

  // Storekeeper path: file a change request for an admin to approve.
  async function requestReferralChange() {
    if (!reqForm.referred_by) return;
    try {
      await api.post(`/clients/clients/${detail.id}/request-referral-change/`, {
        referred_by: reqForm.referred_by,
        reason: reqForm.reason,
      });
      const fresh = await api.get(`/clients/clients/${detail.id}/`);
      setDetail(fresh.data);
      setReqForm({ referred_by: "", reason: "" });
      toast(t("clients.referralRequestSent"));
    } catch (e) {
      toast(errMsg(e), "error");
    }
  }

  const columns = [
    {
      key: "display_name",
      label: t("common.name"),
      sortKey: "sort_name",
      // Второй строкой — то, чего нет в основном названии: у юрлица контактное
      // лицо, у физлица компания, от которой он заказывает. Раньше одно из двух
      // просто не было видно, хотя в базе хранилось.
      render: (c) => {
        const second = c.type === "OSOO" ? c.full_name : c.company_name;
        return (
          <>
            <strong>{c.display_name}</strong>
            {second && second !== c.display_name && (
              <div className="muted" style={{ fontSize: 12 }}>{second}</div>
            )}
          </>
        );
      },
    },
    {
      key: "type",
      label: t("clients.type"),
      render: (c) => <span className="chip">{c.type === "OSOO" ? t("clients.osoo") : t("clients.physical")}</span>,
    },
    { key: "phone", label: t("clients.phone") },
    {
      key: "orders_count",
      label: t("clients.orders"),
      sortKey: "orders_count",
      render: (c) =>
        c.orders_count > 0 ? <strong>{c.orders_count}</strong> : <span className="muted">—</span>,
    },
    {
      key: "referrals_count",
      label: t("clients.referralsCol"),
      render: (c) =>
        c.referrals_count > 0 ? (
          <span className="badge blue" style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <Icon name="users" size={13} /> {c.referrals_count}
          </span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      key: "debt",
      label: t("receipts.debt"),
      sortKey: "debt",
      render: (c) =>
        Number(c.debt) > 0 ? (
          <span style={{ color: "var(--danger)", fontWeight: 600 }}>
            {Number(c.debt).toLocaleString("ru-RU")} сом
          </span>
        ) : (
          // Ноль долга — не достижение, а обычное состояние: приглушённый
          // прочерк вместо зелёного «0» без единицы рядом с «540 сом».
          <span className="muted">—</span>
        ),
    },
    // Сдача — сколько ЦЕХ должен клиенту. Соседняя колонка к долгу: вопрос
    // «кто кому остался должен» имеет две стороны, и вторую тоже надо видеть.
    {
      key: "change_due",
      label: t("clients.changeDue"),
      sortKey: "change_due_total",
      render: (c) =>
        Number(c.change_due) > 0 ? (
          <span style={{ color: "var(--accent-strong)", fontWeight: 600 }}>
            {Number(c.change_due).toLocaleString("ru-RU")} сом
          </span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    {
      key: "telegram",
      label: t("clients.telegram"),
      render: (c) => (
        <span className={`badge ${c.is_telegram_linked ? "ok" : ""}`}>
          {c.is_telegram_linked ? t("clients.linked") : t("clients.notLinked")}
        </span>
      ),
    },
    {
      key: "actions",
      label: t("common.actions"),
      render: (c) => (
        <button className="ghost" onClick={() => openDetail(c)} aria-label={t("common.edit")}>
          <Icon name="arrow-right" size={18} />
        </button>
      ),
    },
  ];

  return (
    <>
      <h1>{t("clients.title")}</h1>
      <div className="toolbar">
        <input
          className="search"
          placeholder={`${t("common.search")} (${t("clients.searchHint")})`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          type="button"
          onClick={() => setCreating({ type: "PHYSICAL", full_name: "", company_name: "", phone: "" })}
        >
          + {t("clients.newClient")}
        </button>
      </div>

      {/* Период: месяц стрелками или конкретный день. Показываем клиентов,
          которые заказывали в это время; «Заказов» тогда — за этот же период. */}
      <div className="toolbar" style={{ alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
        <MonthPicker value={period} onChange={(v) => { setPeriod(v); setDay(""); }} />
        <div className="field" style={{ margin: 0 }}>
          <label>{t("clients.filterDay")}</label>
          <input type="date" value={day} onChange={(e) => setDay(e.target.value)} />
        </div>
        <div className="field" style={{ margin: 0, width: 130 }}>
          <label>{t("clients.minOrders")}</label>
          <input
            type="number"
            min="0"
            value={minOrders}
            onChange={(e) => setMinOrders(e.target.value)}
            placeholder="0"
          />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label>{t("clients.debtFilter")}</label>
          <div className="row" style={{ margin: 0, gap: 8 }}>
            <button
              type="button"
              className={onlyDebt ? "" : "secondary"}
              onClick={() => setOnlyDebt((v) => !v)}
            >
              {t("clients.onlyDebtors")}
            </button>
            {/* Сдача — зеркало долга, поэтому фильтр стоит той же парой. */}
            <button
              type="button"
              className={onlyChange ? "" : "secondary"}
              onClick={() => setOnlyChange((v) => !v)}
            >
              {t("clients.onlyChange")}
            </button>
          </div>
        </div>
        {(day || period.month || onlyDebt || onlyChange || minOrders) && (
          <button
            className="ghost"
            onClick={() => {
              setDay("");
              setPeriod({ ...period, month: null });
              setOnlyDebt(false);
              setOnlyChange(false);
              setMinOrders("");
            }}
          >
            {t("common.reset")}
          </button>
        )}
      </div>
      {(day || period.month) && (
        <p className="muted" style={{ fontSize: 13, marginTop: -4 }}>{t("clients.periodHint")}</p>
      )}

      <DataTable columns={columns} rows={clients} sort={sort} onSort={onSort} />

      {detail && (
        <Modal title={detail.display_name} onClose={() => setDetail(null)}>
          <div className="crow">
            <span className="k">{t("clients.phone")}</span>
            <span>{detail.phone}</span>
          </div>
          <div className="crow">
            <span className="k">{t("clients.type")}</span>
            <span>{detail.type === "OSOO" ? t("clients.osoo") : t("clients.physical")}</span>
          </div>
          {/* ИНН — только у юрлица и только ради счёта на оплату. Правится
              прямо здесь: клиента завели давно, а счёт понадобился сегодня. */}
          {detail.type === "OSOO" && (
            <div className="crow">
              <span className="k">{t("clients.inn")}</span>
              {isAdmin ? (
                <input
                  defaultValue={detail.inn || ""}
                  placeholder={t("clients.innPh")}
                  style={{ width: 200, height: 34, textAlign: "right" }}
                  onBlur={(e) => saveInn(e.target.value)}
                />
              ) : (
                <span>{detail.inn || "—"}</span>
              )}
            </div>
          )}
          <div className="crow">
            <span className="k">{t("clients.telegram")}</span>
            <span>{detail.is_telegram_linked ? t("clients.linked") : t("clients.notLinked")}</span>
          </div>
          <div className="crow">
            <span className="k">{t("clients.orders")}</span>
            <span>{detail.stats?.orders_count}</span>
          </div>
          <div className="crow">
            <span className="k">{t("clients.ltv")}</span>
            <span><strong>{Number(detail.stats?.lifetime_value || 0).toLocaleString("ru-RU")} сом</strong></span>
          </div>
          <div className="crow">
            <span className="k">{t("receipts.debt")}</span>
            <span className="row" style={{ gap: 8, alignItems: "center", margin: 0 }}>
              {Number(detail.debt) > 0 ? (
                <strong style={{ color: "var(--danger)" }}>{Number(detail.debt).toLocaleString("ru-RU")} сом</strong>
              ) : (
                <span className="paid">0</span>
              )}
              {/* Общая выплата: клиент гасит несколько заказов одной суммой.
                  Деньги — за админом, как и оплата по отдельному чеку. */}
              {isAdmin && Number(detail.debt) > 0 && (
                <button
                  type="button"
                  className="secondary"
                  style={{ padding: "3px 9px", height: "auto", fontSize: 12, whiteSpace: "nowrap" }}
                  onClick={() => setPayingClient(detail)}
                >
                  {t("clients.bulkPay")}
                </button>
              )}
            </span>
          </div>
          {/* Сдача показывается ТОЛЬКО когда она есть: строка «сдача 0» у
              каждого клиента — шум, а не информация. Выдаётся она в «Чеках», по
              тому заказу, где переплатили: там видно, за что именно. */}
          {Number(detail.change_due) > 0 && (
            <div className="crow">
              <span className="k">{t("clients.changeDue")}</span>
              <strong style={{ color: "var(--accent-strong)" }}>
                {Number(detail.change_due).toLocaleString("ru-RU")} сом
              </strong>
            </div>
          )}
          <div className="crow">
            <span className="k">{t("clients.portalPass")}</span>
            <span className="row" style={{ gap: 8, alignItems: "center", margin: 0 }}>
              {detail.has_password ? (
                <span className="badge ok">{t("clients.passSet")}</span>
              ) : (
                <span className="muted">{t("clients.passNotSet")}</span>
              )}
              {/* Выдавать пароль может только админ — складовщик видит статус. */}
              {isAdmin && (
                <button
                  type="button"
                  className="ghost"
                  style={{ padding: "3px 8px", height: "auto", fontSize: 12, color: "var(--accent-strong)" }}
                  onClick={issuePassword}
                >
                  {detail.has_password ? t("clients.reissuePass") : t("clients.issuePass")}
                </button>
              )}
            </span>
          </div>

          {/* Заказы клиента — что покупал */}
          <div className="field" style={{ marginTop: 14 }}>
            <label>{t("clients.ordersList")}</label>
            {detail.orders?.length ? (
              // При десятках заказов карточка превращалась в бесконечную ленту:
              // показываем последние ORDERS_PREVIEW, остальное — по кнопке.
              (showAllOrders ? detail.orders : detail.orders.slice(0, ORDERS_PREVIEW)).map((o) => (
                <div className="card" key={o.id} style={{ background: "var(--canvas)", padding: 10, marginBottom: 6 }}>
                  <div className="crow">
                    <strong>
                      №{o.order_number}
                      {o.title ? <span className="muted" style={{ fontWeight: 400 }}> · {o.title}</span> : null}
                    </strong>
                    <span className="muted">{new Date(o.created_at).toLocaleDateString("ru-RU")}</span>
                  </div>
                  {o.items.map((it, i) => (
                    <div className="crow" key={i} style={{ fontSize: 13 }}>
                      <span className="k">{it.title} × {Number(it.quantity)}</span>
                      <span>{Number(it.line_total).toLocaleString("ru-RU")} сом</span>
                    </div>
                  ))}
                  <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 4, paddingTop: 4 }}>
                    <strong>{Number(o.total_price).toLocaleString("ru-RU")} сом</strong>
                    {Number(o.debt) > 0 && (
                      <span style={{ color: "var(--danger)", fontSize: 13 }}>
                        {t("receipts.debt")}: {Number(o.debt).toLocaleString("ru-RU")}
                      </span>
                    )}
                    {Number(o.change_due) > 0 && (
                      <span style={{ color: "var(--accent-strong)", fontSize: 13 }}>
                        {t("receipts.change")}: {Number(o.change_due).toLocaleString("ru-RU")}
                      </span>
                    )}
                  </div>
                </div>
              ))
            ) : (
              <span className="muted">{t("common.empty")}</span>
            )}
            {detail.orders?.length > ORDERS_PREVIEW && (
              <button
                className="ghost"
                style={{ color: "var(--accent-strong)" }}
                onClick={() => setShowAllOrders((v) => !v)}
              >
                {showAllOrders
                  ? t("clients.ordersCollapse")
                  : t("clients.ordersShowAll", { count: detail.orders.length })}
              </button>
            )}
          </div>

          {/* История оплат: когда и сколько клиент реально принёс. По полю
              «оплачено» на заказе этого не видно — общая выплата расходится
              сразу по нескольким заказам, а дата может быть задним числом. */}
          {detail.payments?.length > 0 && (
            <div className="field" style={{ marginTop: 14 }}>
              <label>{t("clients.paymentsList")}</label>
              {(showAllPayments ? detail.payments : detail.payments.slice(0, PAYMENTS_PREVIEW)).map((p) => (
                <div className="crow" key={p.id} style={{ fontSize: 13 }}>
                  <span>
                    <span className="muted">{new Date(p.paid_on).toLocaleDateString("ru-RU")}</span>
                    {" · "}
                    №{p.order_number}
                    {p.order_title ? <span className="muted"> · {p.order_title}</span> : null}
                  </span>
                  <span>
                    <strong>{Math.round(Number(p.amount)).toLocaleString("ru-RU")} сом</strong>
                    <span className="muted" style={{ fontSize: 12 }}> · {p.method_display}</span>
                  </span>
                </div>
              ))}
              {detail.payments.length > PAYMENTS_PREVIEW && (
                <button
                  className="ghost"
                  style={{ color: "var(--accent-strong)" }}
                  onClick={() => setShowAllPayments((v) => !v)}
                >
                  {showAllPayments
                    ? t("clients.ordersCollapse")
                    : t("clients.paymentsShowAll", { count: detail.payments.length })}
                </button>
              )}
            </div>
          )}

          {/* Who referred this client. Free to set once; changing a locked
              referral needs admin override or a moderated change request. */}
          <div className="field" style={{ marginTop: 14 }}>
            <label>{t("clients.referredByLabel")}</label>
            {!detail.referred_by ? (
              // Not set yet → anyone can pick once.
              <select value="" onChange={(e) => setReferrer(e.target.value)}>
                <option value="">— {t("clients.noReferrer")} —</option>
                {clients
                  .filter((c) => c.id !== detail.id)
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.display_name} ({c.phone})
                    </option>
                  ))}
              </select>
            ) : isAdmin ? (
              // Admin override → edit directly.
              <select value={detail.referred_by} onChange={(e) => setReferrer(e.target.value)}>
                <option value="">— {t("clients.noReferrer")} —</option>
                {clients
                  .filter((c) => c.id !== detail.id)
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.display_name} ({c.phone})
                    </option>
                  ))}
              </select>
            ) : (
              // Storekeeper → locked; can file a change request.
              <>
                <div className="crow" style={{ padding: "8px 0" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Icon name="lock" size={15} /> {detail.referred_by_name}
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>{t("clients.referralLocked")}</span>
                </div>
                {detail.pending_referral_request ? (
                  <div className="badge" style={{ marginTop: 6, display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Icon name="clock" size={14} /> {t("clients.referralChangePending")}: {detail.pending_referral_request.new_referred_by_name || "—"}
                  </div>
                ) : (
                  <div className="card" style={{ background: "var(--canvas)", padding: 12, marginTop: 6 }}>
                    <label style={{ fontSize: 12 }}>{t("clients.requestReferralChange")}</label>
                    <select
                      value={reqForm.referred_by}
                      onChange={(e) => setReqForm({ ...reqForm, referred_by: e.target.value })}
                    >
                      <option value="">— {t("clients.noReferrer")} —</option>
                      {clients
                        .filter((c) => c.id !== detail.id && c.id !== detail.referred_by)
                        .map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.display_name} ({c.phone})
                          </option>
                        ))}
                    </select>
                    <input
                      style={{ marginTop: 6 }}
                      placeholder={t("clients.referralChangeReason")}
                      value={reqForm.reason}
                      onChange={(e) => setReqForm({ ...reqForm, reason: e.target.value })}
                    />
                    <button
                      style={{ marginTop: 8 }}
                      disabled={!reqForm.referred_by}
                      onClick={requestReferralChange}
                    >
                      {t("clients.requestReferralChange")}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>

          {/* Clients this one referred */}
          <div className="field" style={{ margin: 0 }}>
            <label>
              {t("clients.referrals")}: {detail.referrals?.count || 0}
              {detail.referrals?.count > 0 && (
                <span className="muted"> · {Number(detail.referrals.total_value).toLocaleString("ru-RU")} сом</span>
              )}
            </label>
            {Number(detail.referrals?.bonus) > 0 && (
              <div
                className="crow"
                style={{
                  background: "var(--primary-soft)",
                  borderRadius: "var(--r-md)",
                  padding: "8px 12px",
                  marginBottom: 6,
                }}
              >
                <strong style={{ color: "var(--accent-strong)" }}>{t("clients.referralBonus")}</strong>
                <strong style={{ color: "var(--accent-strong)" }}>
                  {Number(detail.referrals.bonus).toLocaleString("ru-RU")} сом
                </strong>
              </div>
            )}
            {detail.referrals?.list?.length ? (
              detail.referrals.list.map((r) => (
                <div className="crow" key={r.id}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Icon name="user" size={15} /> {r.display_name}
                  </span>
                  <span className="muted">{Number(r.lifetime_value).toLocaleString("ru-RU")} сом</span>
                </div>
              ))
            ) : (
              <span className="muted">{t("common.empty")}</span>
            )}
          </div>

          {/* Акт сверки — тем же документом, что и в 1С, закрывают спор о долге
              с юрлицом. Данные уже в карточке, форма собирается из них. */}
          <div className="row" style={{ marginTop: 16 }}>
            <button className="secondary" onClick={() => setActFor(detail)}>
              <Icon name="printer" size={16} /> {t("print.actTitle")}
            </button>
          </div>

          {/* Склейка двойников. Один человек, заведённый дважды (номер записали
              в разном формате), имел две карточки — и его заказы с долгом лежали
              двумя стопками. Прячем под раскрывашку: карточка удаляется
              безвозвратно, такому не место рядом с обычными полями. */}
          {isAdmin && (
            <details style={{ marginTop: 18 }} onToggle={() => setMerging(null)}>
              <summary style={{ cursor: "pointer", fontSize: 13, color: "var(--ink-muted)" }}>
                {t("clients.mergeTitle")}
              </summary>
              <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>{t("clients.mergeHint")}</p>
              <select
                value={merging?.from || ""}
                onChange={(e) => previewMerge(e.target.value)}
              >
                <option value="">— {t("clients.mergePick")} —</option>
                {clients
                  .filter((c) => c.id !== detail.id)
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.display_name} ({c.phone})
                    </option>
                  ))}
              </select>

              {merging?.preview && (
                <div className="card" style={{ background: "var(--canvas)", padding: 12, marginTop: 8 }}>
                  <div className="crow">
                    <span className="k">{t("clients.mergeOrders")}</span>
                    <strong>{merging.preview.orders}</strong>
                  </div>
                  {Number(merging.preview.debt) > 0 && (
                    <div className="crow">
                      <span className="k">{t("receipts.debt")}</span>
                      <strong style={{ color: "var(--danger)" }}>
                        {Number(merging.preview.debt).toLocaleString("ru-RU")} сом
                      </strong>
                    </div>
                  )}
                  {merging.preview.referrals > 0 && (
                    <div className="crow">
                      <span className="k">{t("clients.referrals")}</span>
                      <strong>{merging.preview.referrals}</strong>
                    </div>
                  )}
                  <button className="danger" style={{ marginTop: 10 }} onClick={doMerge}>
                    {t("clients.mergeAction", { name: merging.preview.drop })}
                  </button>
                </div>
              )}
            </details>
          )}
        </Modal>
      )}

      {creating && (
        <Modal
          title={t("clients.newClient")}
          onClose={() => setCreating(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setCreating(null)}>{t("common.cancel")}</button>
              <button onClick={createClient}>{t("common.add")}</button>
            </>
          }
        >
          <div className="field">
            <label>{t("clients.type")}</label>
            <select value={creating.type} onChange={(e) => setCreating({ ...creating, type: e.target.value })}>
              <option value="PHYSICAL">{t("clients.physical")}</option>
              <option value="OSOO">{t("clients.osoo")}</option>
            </select>
          </div>
          {/* ФИО и компания — оба поля, а не «или-или».
              Раньше форма показывала одно вместо другого: у ОсОО нельзя было
              записать контактное лицо (кому звонить по заказу), а у физлица —
              компанию, от которой он заказывает. При этом в базе есть оба поля
              и заполнены оба — форма просто не давала их ввести. */}
          <div className="field">
            <label>{t("clients.fullName")}</label>
            <input
              value={creating.full_name}
              onChange={(e) => setCreating({ ...creating, full_name: e.target.value })}
              placeholder={creating.type === "OSOO" ? t("clients.contactPh") : ""}
              autoFocus
            />
          </div>
          <div className="field">
            <label>
              {t("clients.companyName")}
              {creating.type !== "OSOO" && (
                <span className="muted"> — {t("common.optional")}</span>
              )}
            </label>
            <input
              value={creating.company_name}
              onChange={(e) => setCreating({ ...creating, company_name: e.target.value })}
              placeholder={t("clients.companyPh")}
            />
          </div>
          <div className="field">
            <label>{t("clients.phone")}</label>
            <input
              value={creating.phone}
              onChange={(e) => setCreating({ ...creating, phone: e.target.value })}
              placeholder="+996…"
              inputMode="tel"
            />
          </div>
          {/* ИНН спрашиваем только у юрлица и только ради счёта на оплату: без
              него бухгалтерия клиента счёт не проведёт. У физлица его нет. */}
          {creating.type === "OSOO" && (
            <div className="field">
              <label>{t("clients.inn")}</label>
              <input
                value={creating.inn ?? ""}
                onChange={(e) => setCreating({ ...creating, inn: e.target.value })}
                placeholder={t("clients.innPh")}
              />
            </div>
          )}
        </Modal>
      )}

      {actFor && <PrintAct client={actFor} onClose={() => setActFor(null)} />}

      {payingClient && (
        <BulkPayModal
          client={payingClient}
          orders={payingClient.orders}
          onClose={() => setPayingClient(null)}
          onPaid={() => {
            setPayingClient(null);
            refreshDetail(payingClient.id);
          }}
        />
      )}

      {issuedPassword && (
        <Modal title={t("clients.passModalTitle")} onClose={() => setIssuedPassword(null)}>
          <p style={{ fontSize: 40, fontWeight: 700, letterSpacing: 4, textAlign: "center", margin: "8px 0" }}>
            {issuedPassword}
          </p>
          <p className="muted" style={{ textAlign: "center" }}>{t("clients.passHint")}</p>
        </Modal>
      )}
    </>
  );
}
