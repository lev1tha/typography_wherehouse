import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Icon from "./Icon.jsx";
import LoadError from "./LoadError.jsx";
import Pager, { usePage } from "./Pager.jsx";
import { useUI } from "./UIProvider.jsx";
import { isCanceled, useLatest } from "../utils/latest.js";
import { formatDateTime } from "../utils/format.js";
import { downloadFile } from "../utils/download.js";

// Вкладка «Журнал действий» (XL-07, F3, STAFF-06): кто, когда и что сделал, с
// «было → стало» по тратам, выплатам зарплаты, кассе, настройкам финансов и
// налога. Фильтры — по дате, пользователю, типу и тексту; страницы по 50.

const KINDS = ["login", "order", "cash", "expense", "payroll", "tax", "settings", "stock", "price", "client", "staff", "other"];
const ICONS = {
  login: "key", order: "receipt", cash: "wallet", expense: "clipboard", payroll: "users", tax: "tag",
  settings: "lock", stock: "package", price: "tag", client: "user", staff: "users", other: "dot",
};
const PAGE = 50;

function useDebounced(value, ms) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setV(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return v;
}

export default function AuditJournal() {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [failed, setFailed] = useState(false);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [username, setUsername] = useState("");
  const [kind, setKind] = useState("");
  const [search, setSearch] = useState("");
  const dUser = useDebounced(username.trim(), 350);
  const dSearch = useDebounced(search.trim(), 350);
  const filters = {
    ...(dateFrom ? { date_from: dateFrom } : {}),
    ...(dateTo ? { date_to: dateTo } : {}),
    ...(dUser ? { username: dUser } : {}),
    ...(kind ? { kind } : {}),
    ...(dSearch ? { search: dSearch } : {}),
  };
  const filtered = Object.keys(filters).length > 0;
  const [page, setPage] = usePage(JSON.stringify(filters));
  const next = useLatest();

  function load() {
    api
      .get("/audit/logs/", { params: { ...filters, page_size: PAGE, ...(page > 1 ? { page } : {}) }, signal: next() })
      .then((r) => {
        setRows(r.data.results);
        setCount(r.data.count ?? r.data.results.length);
        setFailed(false);
      })
      .catch((e) => {
        if (isCanceled(e)) return;
        if (e.response?.status === 404 && page > 1) return setPage(1);
        setFailed(true);
        toast(apiError(e, t("common.loadFailed")), "error");
      });
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [page, JSON.stringify(filters)]);

  function reset() {
    setDateFrom(""); setDateTo(""); setUsername(""); setKind(""); setSearch("");
  }

  if (failed && !rows.length) return <LoadError onRetry={load} />;

  return (
    <>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        <input type="date" value={dateFrom} aria-label={t("auditLog.from")} title={t("auditLog.from")}
          max={dateTo || undefined} onChange={(e) => setDateFrom(e.target.value)} />
        <input type="date" value={dateTo} aria-label={t("auditLog.to")} title={t("auditLog.to")}
          min={dateFrom || undefined} onChange={(e) => setDateTo(e.target.value)} />
        <input value={username} style={{ width: 150 }} placeholder={t("auditLog.user")} aria-label={t("auditLog.user")}
          onChange={(e) => setUsername(e.target.value)} />
        <select aria-label={t("auditLog.kind")} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">{t("auditLog.allKinds")}</option>
          {KINDS.map((k) => <option key={k} value={k}>{t(`auditLog.kind_${k}`)}</option>)}
        </select>
        <input className="search" type="search" value={search} placeholder={t("auditLog.searchPh")}
          aria-label={t("common.search")} onChange={(e) => setSearch(e.target.value)} />
        {filtered && <button type="button" className="secondary" onClick={reset}>{t("common.resetFilters")}</button>}
        {/* CSV с теми же фильтрами — все страницы сразу (волна 2). */}
        <button
          type="button"
          className="secondary"
          onClick={() =>
            downloadFile("/audit/logs/export/", filters, "zhurnal.csv").catch((e) =>
              toast(apiError(e, t("common.error")), "error")
            )
          }
        >
          {t("clients.exportCsv")}
        </button>
      </div>

      {!rows.length ? (
        <div className="empty-state">
          <Icon name="archive" size={40} className="es-icon" />
          {filtered ? t("common.emptyFiltered") : t("common.empty")}
        </div>
      ) : (
        <div className="feed">
          {rows.map((r) => (
            <div className="feed-item" key={r.id}>
              <div className="feed-icon"><Icon name={ICONS[r.kind] || "dot"} size={17} /></div>
              <div className="feed-body">
                <div className="feed-action">{r.action}</div>
                <div className="feed-meta">
                  {r.username || "—"} · {formatDateTime(r.created_at)} · {t(`auditLog.kind_${r.kind}`, { defaultValue: r.kind })}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      <Pager page={page} count={count} pageSize={PAGE} onPage={setPage} />
    </>
  );
}
