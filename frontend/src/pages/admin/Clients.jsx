import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import { useUI } from "../../components/UIProvider.jsx";

export default function Clients() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin } = useAuth();
  const [clients, setClients] = useState([]);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState(null);
  const [reqForm, setReqForm] = useState({ referred_by: "", reason: "" });

  function load() {
    const params = search ? { search } : {};
    api.get("/clients/clients/", { params }).then((r) => setClients(r.data.results));
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  async function openDetail(c) {
    const { data } = await api.get(`/clients/clients/${c.id}/`);
    setDetail(data);
    setReqForm({ referred_by: "", reason: "" });
  }

  async function resetPassword() {
    if (!(await confirm(t("clients.resetPassConfirm")))) return;
    try {
      await api.post(`/clients/clients/${detail.id}/reset-password/`, {});
      const { data } = await api.get(`/clients/clients/${detail.id}/`);
      setDetail(data);
      toast(t("clients.resetPassDone"));
    } catch {
      toast(t("common.error"), "error");
    }
  }

  function errMsg(e) {
    const data = e.response?.data;
    return (
      data?.detail ||
      (Array.isArray(data?.referred_by) ? data.referred_by[0] : null) ||
      t("common.error")
    );
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
    { key: "display_name", label: t("common.name"), render: (c) => <strong>{c.display_name}</strong> },
    {
      key: "type",
      label: t("clients.type"),
      render: (c) => <span className="chip">{c.type === "OSOO" ? t("clients.osoo") : t("clients.physical")}</span>,
    },
    { key: "phone", label: t("clients.phone") },
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
      render: (c) =>
        Number(c.debt) > 0 ? (
          <span style={{ color: "var(--danger)", fontWeight: 600 }}>
            {Number(c.debt).toLocaleString("ru-RU")} сом
          </span>
        ) : (
          <span className="paid" style={{ color: "var(--ok, #067647)" }}>0</span>
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
      </div>
      <DataTable columns={columns} rows={clients} />

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
                <>
                  <span className="badge ok">{t("clients.passSet")}</span>
                  <button
                    className="ghost"
                    style={{ padding: "3px 8px", height: "auto", fontSize: 12, color: "var(--accent-strong)" }}
                    onClick={resetPassword}
                  >
                    {t("clients.resetPass")}
                  </button>
                </>
              ) : (
                <span className="muted">{t("clients.passNotSet")}</span>
              )}
            </span>
          </div>

          {/* Заказы клиента — что покупал */}
          <div className="field" style={{ marginTop: 14 }}>
            <label>{t("clients.ordersList")}</label>
            {detail.orders?.length ? (
              detail.orders.map((o) => (
                <div className="card" key={o.id} style={{ background: "var(--canvas)", padding: 10, marginBottom: 6 }}>
                  <div className="crow">
                    <strong>№{o.order_number}</strong>
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
    </>
  );
}
