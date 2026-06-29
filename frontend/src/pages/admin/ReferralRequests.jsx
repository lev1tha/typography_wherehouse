import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import DataTable from "../../components/DataTable.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const STATUSES = ["PENDING", "APPROVED", "REJECTED"];

export default function ReferralRequests() {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [rows, setRows] = useState([]);
  const [statusFilter, setStatusFilter] = useState("PENDING");

  function load() {
    api
      .get("/clients/referral-requests/", { params: { status: statusFilter } })
      .then((r) => setRows(r.data.results));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  async function approve(id) {
    try {
      await api.post(`/clients/referral-requests/${id}/approve/`);
      toast(t("clients.referralApproved"));
      load();
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    }
  }

  async function reject(id) {
    const reason = window.prompt(t("clients.rejectReason")) ?? "";
    try {
      await api.post(`/clients/referral-requests/${id}/reject/`, { reason });
      toast(t("clients.referralRejected"));
      load();
    } catch (e) {
      toast(e.response?.data?.detail || t("common.error"), "error");
    }
  }

  const columns = [
    { key: "client_name", label: t("clients.title"), render: (r) => <strong>{r.client_name}</strong> },
    {
      key: "change",
      label: t("clients.referredByLabel"),
      render: (r) => (
        <span>
          {r.previous_referred_by_name || "—"} <span className="muted">→</span>{" "}
          <strong>{r.new_referred_by_name || "—"}</strong>
        </span>
      ),
    },
    { key: "requested_by_name", label: t("clients.requestedBy") },
    { key: "reason", label: t("clients.referralChangeReason"), render: (r) => r.reason || <span className="muted">—</span> },
    {
      key: "created_at",
      label: t("clients.date"),
      render: (r) => new Date(r.created_at).toLocaleString("ru-RU"),
    },
    {
      key: "actions",
      label: t("common.actions"),
      render: (r) =>
        r.status === "PENDING" ? (
          <div className="row" style={{ gap: 6 }}>
            <button onClick={() => approve(r.id)}>{t("clients.approve")}</button>
            <button className="secondary" onClick={() => reject(r.id)}>{t("clients.reject")}</button>
          </div>
        ) : (
          <span className={`badge ${r.status === "APPROVED" ? "ok" : ""}`}>{r.status_display}</span>
        ),
    },
  ];

  return (
    <>
      <h1>{t("clients.referralRequestsTitle")}</h1>
      <div className="toolbar">
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {t(`clients.status${s}`)}
            </option>
          ))}
        </select>
      </div>
      <DataTable columns={columns} rows={rows} />
    </>
  );
}
