import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";

// Сколько заказов показывать в карточке сразу — остальные под кнопкой.
const ORDERS_PREVIEW = 5;

export default function Clients() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin } = useAuth();
  const [clients, setClients] = useState([]);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState(null);
  const [reqForm, setReqForm] = useState({ referred_by: "", reason: "" });
  const [issuedPassword, setIssuedPassword] = useState(null); // показывается один раз
  const [period, setPeriod] = useState({ year: new Date().getFullYear(), month: null });
  const [day, setDay] = useState(""); // конкретный день внутри месяца
  const [sort, setSort] = useState({ key: "sort_name", dir: "asc" });
  const [showAllOrders, setShowAllOrders] = useState(false);
  const [onlyDebt, setOnlyDebt] = useState(false);
  const [minOrders, setMinOrders] = useState("");
  // Клиента можно завести заранее, не дожидаясь продажи.
  const [creating, setCreating] = useState(null);

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
      ...(Number(minOrders) > 0 ? { min_orders: Number(minOrders) } : {}),
      ordering: (sort.dir === "desc" ? "-" : "") + sort.key,
    };
    api.get("/clients/clients/", { params }).then((r) => setClients(r.data.results));
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search, period.year, period.month, day, onlyDebt, minOrders, sort.key, sort.dir]);

  function onSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));
  }

  async function openDetail(c) {
    const { data } = await api.get(`/clients/clients/${c.id}/`, { params: periodParams() });
    setDetail(data);
    setShowAllOrders(false);
    setReqForm({ referred_by: "", reason: "" });
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
    { key: "display_name", label: t("common.name"), sortKey: "sort_name", render: (c) => <strong>{c.display_name}</strong> },
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
          <button
            type="button"
            className={onlyDebt ? "" : "secondary"}
            onClick={() => setOnlyDebt((v) => !v)}
          >
            {t("clients.onlyDebtors")}
          </button>
        </div>
        {(day || period.month || onlyDebt || minOrders) && (
          <button
            className="ghost"
            onClick={() => {
              setDay("");
              setPeriod({ ...period, month: null });
              setOnlyDebt(false);
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
            <span>
              {Number(detail.debt) > 0 ? (
                <strong style={{ color: "var(--danger)" }}>{Number(detail.debt).toLocaleString("ru-RU")} сом</strong>
              ) : (
                <span className="paid">0</span>
              )}
            </span>
          </div>
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
          <div className="field">
            <label>{creating.type === "OSOO" ? t("clients.companyName") : t("clients.fullName")}</label>
            {creating.type === "OSOO" ? (
              <input
                value={creating.company_name}
                onChange={(e) => setCreating({ ...creating, company_name: e.target.value })}
                autoFocus
              />
            ) : (
              <input
                value={creating.full_name}
                onChange={(e) => setCreating({ ...creating, full_name: e.target.value })}
                autoFocus
              />
            )}
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
        </Modal>
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
