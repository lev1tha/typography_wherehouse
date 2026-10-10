import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useUI } from "./UIProvider.jsx";
import DataTable from "./DataTable.jsx";
import Modal from "./Modal.jsx";
import { formatDate, formatMoney } from "../utils/format.js";

// Список коммерческих предложений (CALC-03): найти, распечатать, отправить в
// корзину («Оформить заказ из КП») или отменить. Само КП ничего не списывает и
// ни на что не влияет, пока заказ из него не оформлен.
export default function QuotesModal({ onClose, onLoad, onPrint }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const [rows, setRows] = useState(null);
  const [search, setSearch] = useState("");

  function load(q = search) {
    api
      .get("/sales/quotes/", { params: { search: q || undefined, page_size: 50 } })
      .then((r) => setRows(r.data.results || r.data))
      .catch((e) => toast(apiError(e, t("common.loadFailed")), "error"));
  }
  useEffect(() => {
    const id = setTimeout(() => load(search), 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);

  async function cancel(q) {
    if (!(await confirm(t("quote.cancelConfirm", { n: q.number })))) return;
    try {
      await api.post(`/sales/quotes/${q.id}/cancel/`);
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const statusLabel = (q) =>
    q.status === "ORDERED"
      ? t("quote.statusOrdered", { n: q.receipt_number ?? "" })
      : q.status === "CANCELLED"
      ? t("quote.statusCancelled")
      : q.is_expired
      ? t("quote.statusExpired")
      : t("quote.statusActive");

  return (
    <Modal title={t("quote.listTitle")} onClose={onClose} wide>
      <input
        type="search"
        aria-label={t("common.search")}
        placeholder={t("quote.searchPh")}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        style={{ marginBottom: 10 }}
      />
      {rows === null && <p className="muted">{t("common.loading")}</p>}
      {rows && rows.length === 0 && <p className="muted">{t("common.empty")}</p>}
      {rows && (
        <DataTable
          rowKey="id"
          rows={rows}
          filtered={!!search}
          onReset={() => setSearch("")}
          columns={[
            { key: "number", label: "№" },
            { key: "created_at", label: t("quote.date"), render: (q) => formatDate(q.created_at) },
            { key: "client_label", label: t("quote.to"), render: (q) => q.client_label || "—" },
            { key: "title", label: t("quote.subject"), render: (q) => q.title || "—" },
            { key: "total_price", label: t("common.total"), render: (q) => formatMoney(q.total_price) },
            {
              key: "status",
              label: t("quote.status"),
              render: (q) => (
                <>
                  <span className={`badge ${q.status === "ACTIVE" && !q.is_expired ? "ok" : ""}`}>{statusLabel(q)}</span>
                  {q.valid_until && q.status === "ACTIVE" && (
                    <div className="muted" style={{ fontSize: 12 }}>
                      {t("quote.until", { date: formatDate(q.valid_until) })}
                    </div>
                  )}
                </>
              ),
            },
            {
              key: "actions",
              label: t("common.actions"),
              render: (q) => (
                <div className="row" style={{ gap: 6, margin: 0, flexWrap: "wrap" }}>
                  <button type="button" className="secondary" onClick={() => onPrint(q)}>{t("print.print")}</button>
                  {q.status === "ACTIVE" && (
                    <>
                      <button type="button" onClick={() => onLoad(q)}>{t("quote.toCart")}</button>
                      <button type="button" className="ghost" onClick={() => cancel(q)}>{t("quote.cancel")}</button>
                    </>
                  )}
                </div>
              ),
            },
          ]}
        />
      )}
    </Modal>
  );
}
