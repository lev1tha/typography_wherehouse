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

// Касса и банк: сколько денег есть сейчас и что с ними происходило.
//
// В системе были только ОБОРОТЫ — выручка, расходы, долги, — и на вопрос
// «сколько сейчас должно быть в ящике» ответить было нечем. Это то, чем в 1С
// закрывают день, поэтому остаток стоит первым, а книга под ним.
//
// Оплаты, сдачу, возвраты и откаты система пишет сама; руками вносят то, чего
// она знать не может: закуп за наличные, зарплату, инкассацию.

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const today = () => new Date().toLocaleDateString("sv-SE");

// Статьи, которые можно вносить руками. Оплаты и сдача сюда не входят: их
// пишет система по чекам, и ручная запись развела бы кассу с продажами.
const MANUAL_ARTICLES = {
  IN: ["DEPOSIT", "TRANSFER", "OTHER"],
  OUT: ["SUPPLY", "EXPENSE", "SALARY", "TRANSFER", "OTHER"],
};

function periodParams({ year, month }) {
  if (!month) return {};
  const last = new Date(year, month, 0).getDate();
  const p = (n) => String(n).padStart(2, "0");
  return { date_from: `${year}-${p(month)}-01`, date_to: `${year}-${p(month)}-${p(last)}` };
}

export default function Cash() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin } = useAuth();
  const now = new Date();
  const [period, setPeriod] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 });
  const [balance, setBalance] = useState(null);
  const [rows, setRows] = useState([]);
  const [account, setAccount] = useState("");
  const [entry, setEntry] = useState(null);   // форма прихода/расхода
  const [counting, setCounting] = useState(null); // пересчёт кассы
  const [busy, setBusy] = useState(false);

  const params = periodParams(period);

  function load() {
    api.get("/finance/cash/balance/", { params })
      .then((r) => setBalance(r.data))
      .catch(() => toast(t("common.error"), "error"));
    api.get("/finance/cash/", { params: { ...params, ...(account ? { account } : {}), page_size: 200 } })
      .then((r) => setRows(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [period.year, period.month, account]);

  function startEntry(kind) {
    setEntry({
      kind,
      account: "CASH",
      article: MANUAL_ARTICLES[kind][0],
      amount: "",
      happened_on: today(),
      note: "",
    });
  }

  async function saveEntry() {
    if (!(Number(entry.amount) > 0)) return toast(t("cash.needAmount"), "error");
    setBusy(true);
    try {
      await api.post("/finance/cash/", { ...entry, amount: Number(entry.amount) });
      setEntry(null);
      load();
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function saveCount() {
    if (counting.counted === "") return toast(t("cash.needCounted"), "error");
    setBusy(true);
    try {
      const { data } = await api.post("/finance/cash/count/", {
        account: counting.account,
        counted: Number(counting.counted),
        note: counting.note,
      });
      setCounting(null);
      load();
      toast(Number(data.diff) === 0 ? t("cash.countMatches") : t("cash.countDiff", { sum: som(data.diff) }));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function removeEntry(row) {
    if (!(await confirm(t("cash.deleteConfirm")))) return;
    try {
      await api.delete(`/finance/cash/${row.id}/`);
      load();
      toast(t("common.saved"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const columns = [
    { key: "happened_on", label: t("cash.date"), render: (r) => new Date(r.happened_on).toLocaleDateString("ru-RU") },
    { key: "account_display", label: t("cash.account") },
    {
      key: "article_display",
      label: t("cash.article"),
      render: (r) => (
        <>
          <span>{r.article_display}</span>
          {r.order_number ? <span className="muted"> · №{r.order_number}</span> : null}
          {r.note ? <div className="muted" style={{ fontSize: 12 }}>{r.note}</div> : null}
        </>
      ),
    },
    {
      key: "in",
      label: t("cash.in"),
      render: (r) => (r.kind === "IN" ? <strong style={{ color: "var(--ok)" }}>{som(r.amount)}</strong> : <span className="muted">—</span>),
    },
    {
      key: "out",
      label: t("cash.out"),
      render: (r) => (r.kind === "OUT" ? <strong style={{ color: "var(--danger)" }}>{som(r.amount)}</strong> : <span className="muted">—</span>),
    },
    {
      key: "who",
      label: t("cash.who"),
      render: (r) =>
        r.is_auto ? (
          <span className="badge">{t("cash.auto")}</span>
        ) : (
          <span className="muted">{r.created_by_name || "—"}</span>
        ),
    },
    ...(isAdmin
      ? [{
          key: "actions",
          label: "",
          // Записи системы не трогаем: они отражают чеки, и правка развела бы
          // кассу с продажами.
          render: (r) =>
            r.is_auto ? null : (
              <button className="ghost row-btn row-danger" onClick={() => removeEntry(r)}>
                <Icon name="trash" size={14} /> {t("common.delete")}
              </button>
            ),
        }]
      : []),
  ];

  if (!balance) return <p className="muted">{t("common.loading")}</p>;

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 12 }}>
        <div>
          <h1 style={{ margin: 0 }}>{t("cash.title")}</h1>
          <p className="muted" style={{ margin: "4px 0 0", fontSize: 13, maxWidth: "62ch" }}>
            {t("cash.hint")}
          </p>
        </div>
        <MonthPicker value={period} onChange={setPeriod} />
      </div>

      {/* Остаток — первым: это то, ради чего сюда заходят. */}
      <div className="stat-grid" style={{ marginTop: 16 }}>
        {balance.accounts.map((a) => (
          <div className="stat" key={a.account}>
            <div className="label">{a.label}</div>
            <div className="value" style={{ color: Number(a.balance) < 0 ? "var(--danger)" : undefined }}>
              {som(a.balance)}
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {t("cash.turnover", { in: som(a.income), out: som(a.outcome) })}
            </div>
          </div>
        ))}
        <div className="stat">
          <div className="label">{t("cash.total")}</div>
          <div className="value" style={{ color: "var(--accent-strong)" }}>{som(balance.total)}</div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("cash.totalHint")}</div>
        </div>
      </div>

      {isAdmin && (
        <div className="row" style={{ marginTop: 14, gap: 8, flexWrap: "wrap" }}>
          <button onClick={() => startEntry("IN")}>+ {t("cash.addIn")}</button>
          <button className="secondary" onClick={() => startEntry("OUT")}>− {t("cash.addOut")}</button>
          <button
            className="secondary"
            onClick={() => setCounting({ account: "CASH", counted: "", note: "" })}
          >
            {t("cash.count")}
          </button>
        </div>
      )}

      <div className="toolbar" style={{ marginTop: 14 }}>
        <select value={account} onChange={(e) => setAccount(e.target.value)}>
          <option value="">{t("cash.allAccounts")}</option>
          {balance.accounts.map((a) => (
            <option key={a.account} value={a.account}>{a.label}</option>
          ))}
        </select>
      </div>

      <DataTable columns={columns} rows={rows} />

      {/* --- Приход / расход руками --- */}
      {entry && (
        <Modal
          title={entry.kind === "IN" ? t("cash.addIn") : t("cash.addOut")}
          onClose={() => setEntry(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setEntry(null)}>{t("common.cancel")}</button>
              <button onClick={saveEntry} disabled={busy}>{t("common.save")}</button>
            </>
          }
        >
          <div className="row">
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("cash.account")}</label>
              <select value={entry.account} onChange={(e) => setEntry({ ...entry, account: e.target.value })}>
                {balance.accounts.map((a) => (
                  <option key={a.account} value={a.account}>{a.label}</option>
                ))}
              </select>
            </div>
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("cash.article")}</label>
              <select value={entry.article} onChange={(e) => setEntry({ ...entry, article: e.target.value })}>
                {MANUAL_ARTICLES[entry.kind].map((a) => (
                  <option key={a} value={a}>{t(`cash.article_${a}`)}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="row">
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("cash.amount")}</label>
              <input
                type="number" step="any" value={entry.amount} autoFocus
                onChange={(e) => setEntry({ ...entry, amount: e.target.value })}
              />
            </div>
            <div className="field" style={{ margin: 0, width: 180 }}>
              <label>{t("cash.date")}</label>
              <input
                type="date" value={entry.happened_on}
                onChange={(e) => setEntry({ ...entry, happened_on: e.target.value })}
              />
            </div>
          </div>
          <div className="field">
            <label>{t("cash.note")}</label>
            <input
              value={entry.note}
              placeholder={t("cash.notePh")}
              onChange={(e) => setEntry({ ...entry, note: e.target.value })}
            />
          </div>
          <p className="muted" style={{ fontSize: 12 }}>{t("cash.manualHint")}</p>
        </Modal>
      )}

      {/* --- Пересчёт кассы --- */}
      {counting && (
        <Modal
          title={t("cash.count")}
          onClose={() => setCounting(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setCounting(null)}>{t("common.cancel")}</button>
              <button onClick={saveCount} disabled={busy}>{t("cash.countSave")}</button>
            </>
          }
        >
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("cash.countHint")}</p>
          <div className="row">
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("cash.account")}</label>
              <select value={counting.account} onChange={(e) => setCounting({ ...counting, account: e.target.value })}>
                {balance.accounts.map((a) => (
                  <option key={a.account} value={a.account}>{a.label}</option>
                ))}
              </select>
            </div>
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("cash.counted")}</label>
              <input
                type="number" step="any" value={counting.counted} autoFocus
                onChange={(e) => setCounting({ ...counting, counted: e.target.value })}
              />
            </div>
          </div>
          {counting.counted !== "" && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
              <div className="crow">
                <span className="k">{t("cash.bySystem")}</span>
                <span>{som(balance.accounts.find((a) => a.account === counting.account)?.balance)}</span>
              </div>
              <div className="crow">
                <span className="k">{t("cash.diff")}</span>
                <strong style={{
                  color:
                    Number(counting.counted) -
                      Number(balance.accounts.find((a) => a.account === counting.account)?.balance || 0) === 0
                      ? "var(--ok)"
                      : "var(--danger)",
                }}>
                  {som(
                    Number(counting.counted) -
                      Number(balance.accounts.find((a) => a.account === counting.account)?.balance || 0)
                  )}
                </strong>
              </div>
            </div>
          )}
          <div className="field">
            <label>{t("cash.note")}</label>
            <input value={counting.note} onChange={(e) => setCounting({ ...counting, note: e.target.value })} />
          </div>
        </Modal>
      )}
    </>
  );
}
