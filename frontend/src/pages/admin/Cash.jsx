import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import DataTable from "../../components/DataTable.jsx";
import Field, { focusFirstInvalid } from "../../components/Field.jsx";
import Icon from "../../components/Icon.jsx";
import LoadError from "../../components/LoadError.jsx";
import Modal from "../../components/Modal.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import Pager, { usePage } from "../../components/Pager.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { downloadFile } from "../../utils/download.js";
import { isCanceled, useLatest } from "../../utils/latest.js";
import { formatDate, formatMoney } from "../../utils/format.js";

// Касса и банк: сколько денег есть сейчас и что с ними происходило.
//
// В системе были только ОБОРОТЫ — выручка, расходы, долги, — и на вопрос
// «сколько сейчас должно быть в ящике» ответить было нечем. Это то, чем в 1С
// закрывают день, поэтому остаток стоит первым, а книга под ним.
//
// Оплаты, сдачу, возвраты, откаты и траты «Финансов» система пишет сама;
// руками вносят то, чего она знать не может: деньги владельца, займы,
// переводы между кассой и банком.

const som = (n) => formatMoney(n);
const today = () => new Date().toLocaleDateString("sv-SE");

// Статьи, которые можно вносить руками. Оплаты и сдача сюда не входят: их
// пишет система по чекам, и ручная запись развела бы кассу с продажами.
// Расход цеха и зарплата — тоже нет (с 2026-09-27): их вносят в «Финансах»,
// иначе деньги уходили бы из кассы мимо ОПиУ. Оплата поставщику — тоже нет
// (с 2026-10-07): руками она не гасила долг за накладную, и его можно было
// оплатить второй раз. Деньги владельца и займы — финансовая деятельность в
// ОДДС; ввод начального остатка — вне потока (это не движение денег).
const MANUAL_ARTICLES = {
  IN: ["DEPOSIT", "LOAN_IN", "TRANSFER", "OPENING", "OTHER"],
  OUT: ["OWNER_OUT", "LOAN_OUT", "TRANSFER", "OPENING", "OTHER"],
};
// Подсказка под выбранной статьёй — там, где её легко перепутать.
const ARTICLE_HINTS = ["LOAN_OUT", "TRANSFER", "OPENING"];

// Значение, которое «успокаивается»: поиск по книге уходит на сервер, когда
// человек перестал печатать, а не на каждую букву.
function useDebounced(value, ms) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setV(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return v;
}

function periodParams({ year, month }) {
  if (!month) return {};
  const last = new Date(year, month, 0).getDate();
  const p = (n) => String(n).padStart(2, "0");
  return { date_from: `${year}-${p(month)}-01`, date_to: `${year}-${p(month)}-${p(last)}` };
}

export default function Cash() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin, isAccountant } = useAuth();
  const now = new Date();
  const [period, setPeriod] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 });
  const [balance, setBalance] = useState(null);
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [rowsFailed, setRowsFailed] = useState(false);
  const [account, setAccount] = useState("");
  // Поиск и фильтры книги, как автофильтр в Excel (cash-05): по тексту
  // (примечание, клиент, телефон, кассир, номер заказа), сумме «от и до»,
  // номеру заказа, отметке «сверено с выпиской», дню.
  const [search, setSearch] = useState("");
  const [amountMin, setAmountMin] = useState("");
  const [amountMax, setAmountMax] = useState("");
  const [reconciled, setReconciled] = useState("");
  const [day, setDay] = useState("");
  const [showDays, setShowDays] = useState(false);
  const [days, setDays] = useState([]);
  const [entryErr, setEntryErr] = useState({});
  const [countErr, setCountErr] = useState({});
  const [entry, setEntry] = useState(null);   // форма прихода/расхода
  const [counting, setCounting] = useState(null); // пересчёт кассы
  const [countBalance, setCountBalance] = useState(null); // остаток на дату пересчёта
  const [busy, setBusy] = useState(false);

  const debouncedSearch = useDebounced(search.trim(), 350);
  const debouncedMin = useDebounced(amountMin, 450);
  const debouncedMax = useDebounced(amountMax, 450);
  // Период книги: месяц, а если выбран день — только он.
  const params = day ? { date_from: day, date_to: day } : periodParams(period);
  const filterParams = {
    ...(account ? { account } : {}),
    ...(debouncedSearch ? { search: debouncedSearch } : {}),
    ...(debouncedMin !== "" ? { amount_min: debouncedMin } : {}),
    ...(debouncedMax !== "" ? { amount_max: debouncedMax } : {}),
    ...(reconciled ? { reconciled } : {}),
  };
  const nextRows = useLatest();
  const nextBalance = useLatest();
  const nextDays = useLatest();
  // Книга постраничная (100 записей): раньше брали первые 200 и молча
  // обрезали остальные — у активного месяца записей больше.
  const PAGE = 100;
  const [page, setPage] = usePage(JSON.stringify([period.year, period.month, day, filterParams]));
  const filtered = !!(account || debouncedSearch || debouncedMin !== "" || debouncedMax !== "" || reconciled || day);

  function load() {
    api.get("/finance/cash/balance/", { params: periodParams(period), signal: nextBalance() })
      .then((r) => setBalance(r.data))
      .catch((e) => { if (!isCanceled(e)) toast(apiError(e, t("common.error")), "error"); });
    api.get("/finance/cash/", {
      params: { ...params, ...filterParams, page_size: PAGE, ...(page > 1 ? { page } : {}) },
      signal: nextRows(),
    })
      .then((r) => {
        setRows(r.data.results || r.data);
        setCount(r.data.count ?? (r.data.results || r.data).length);
        setRowsFailed(false);
      })
      .catch((e) => {
        if (isCanceled(e)) return;
        if (e.response?.status === 404 && page > 1) return setPage(1);
        setRowsFailed(true);
        toast(apiError(e, t("common.error")), "error");
      });
    if (showDays) {
      api.get("/finance/cash/day-totals/", { params, signal: nextDays() })
        .then((r) => setDays(r.data.results || []))
        .catch((e) => { if (!isCanceled(e)) toast(apiError(e, t("common.error")), "error"); });
    }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [period.year, period.month, day, account, debouncedSearch, debouncedMin, debouncedMax, reconciled, page, showDays]);

  // Остаток на дату пересчёта: пересчитывать можно задним числом, и «по системе»
  // должно быть на тот день, а не на сегодня.
  useEffect(() => {
    if (!counting) return;
    api.get("/finance/cash/balance/", { params: { date_to: counting.happened_on } })
      .then((r) => setCountBalance(r.data))
      .catch(() => setCountBalance(null));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [counting?.happened_on, !!counting]);

  function resetFilters() {
    setAccount(""); setSearch(""); setAmountMin(""); setAmountMax(""); setReconciled(""); setDay("");
  }

  function exportCsv() {
    downloadFile("/finance/cash/export/", { ...params, ...filterParams }, "kassa.csv")
      .catch((e) => toast(apiError(e, t("common.error")), "error"));
  }

  async function setMark(row, value) {
    try {
      await api.post("/finance/cash/reconcile/", { ids: [row.id], reconciled: value });
      setRows((list) => list.map((r) => (r.id === row.id ? { ...r, reconciled: value } : r)));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  function startEntry(kind) {
    setEntryErr({});
    setEntry({
      kind,
      account: "CASH",
      article: MANUAL_ARTICLES[kind][0],
      amount: "",
      happened_on: today(),
      note: "",
    });
  }

  async function saveEntry(confirmNegative = false) {
    if (!(Number(entry.amount) > 0)) {
      setEntryErr({ amount: t("cash.needAmount") });
      return focusFirstInvalid();
    }
    setEntryErr({});
    setBusy(true);
    try {
      const { data } = await api.post("/finance/cash/", {
        ...entry,
        amount: Number(entry.amount),
        ...(confirmNegative ? { confirm_negative: true } : {}),
      });
      setEntry(null);
      load();
      toast(t("common.saved"));
      // Расход «Прочее» уменьшил кассу, но не попал в ОПиУ — сервер напоминает.
      if (data?.warnings?.length) toast(t("cash.otherNotInPnlSaved"), "warning");
    } catch (e) {
      // Выдача больше остатка — почти всегда лишний ноль или не тот счёт,
      // поэтому сервер сперва переспрашивает. Но кассу вносят и не по порядку
      // (расходы за неделю сегодня, приходы завтра), так что это вопрос, а не
      // запрет: подтвердил — записали как есть.
      const short = e.response?.data?.confirm_negative;
      if (short && !confirmNegative) {
        setBusy(false);
        if (await confirm(String(short))) return saveEntry(true);
        return;
      }
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  async function saveCount() {
    if (counting.counted === "") {
      setCountErr({ counted: t("cash.needCounted") });
      return focusFirstInvalid();
    }
    setCountErr({});
    setBusy(true);
    try {
      const { data } = await api.post("/finance/cash/count/", {
        account: counting.account,
        counted: Number(counting.counted),
        happened_on: counting.happened_on,
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
    { key: "happened_on", label: t("cash.date"), render: (r) => formatDate(r.happened_on) },
    { key: "account_display", label: t("cash.account") },
    {
      key: "article_display",
      label: t("cash.article"),
      render: (r) => (
        <>
          <span>{r.article_display}</span>
          {r.order_number ? <span className="muted"> · №{r.order_number}</span> : null}
          {r.client_name ? <span className="muted"> · {r.client_name}</span> : null}
          {r.note ? <div className="muted" style={{ fontSize: 12 }}>{r.note}</div> : null}
        </>
      ),
    },
    {
      key: "in",
      label: t("cash.in"),
      render: (r) => (r.kind === "IN" ? <strong style={{ color: "var(--ok-ink)" }}>{som(r.amount)}</strong> : <span className="muted">—</span>),
    },
    {
      key: "out",
      label: t("cash.out"),
      render: (r) => (r.kind === "OUT" ? <strong style={{ color: "var(--danger-ink)" }}>{som(r.amount)}</strong> : <span className="muted">—</span>),
    },
    {
      // Остаток счёта сразу после операции — как колонка «=E2+C3−D3» в Excel:
      // по ней находят день и запись, где касса разошлась с ящиком.
      key: "balance_after",
      label: t("cashBook.balanceAfter"),
      render: (r) => (
        <span style={{ color: Number(r.balance_after) < 0 ? "var(--danger-ink)" : undefined }}>
          {som(r.balance_after)}
        </span>
      ),
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
    {
      // «Сверено с выпиской»: отметка владельца при сверке банка. На деньги и
      // отчёты не влияет. Ставит админ; бухгалтер видит.
      key: "reconciled",
      label: t("cashBook.reconciled"),
      render: (r) => (
        <input
          type="checkbox"
          style={{ width: 20, height: 20, minHeight: 0 }}
          checked={!!r.reconciled}
          disabled={!isAdmin}
          aria-label={t("cashBook.reconciled")}
          onChange={(e) => setMark(r, e.target.checked)}
        />
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
            <div className="value" style={{ color: Number(a.balance) < 0 ? "var(--danger-ink)" : undefined }}>
              {som(a.balance)}
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              {t("cash.turnover", { in: som(a.income), out: som(a.outcome) })}
            </div>
          </div>
        ))}
        <div className="stat">
          <div className="label">{t("cash.total")}</div>
          <div className="value" style={{ color: "var(--accent-ink)" }}>{som(balance.total)}</div>
          <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>{t("cash.totalHint")}</div>
          {/* Сдача, которую ещё не вернули, лежит здесь же, но выручкой не
              стала: она уйдёт на руки или в зачёт следующего заказа. Без этой
              строки касса спорила с «Финансами» ровно на неё. */}
          {Number(balance.change_held || 0) > 0 && (
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {t("cash.changeHeld", { value: som(balance.change_held) })}
            </div>
          )}
        </div>
      </div>

      {isAdmin && (
        <div className="row" style={{ marginTop: 14, gap: 8, flexWrap: "wrap" }}>
          <button onClick={() => startEntry("IN")}>+ {t("cash.addIn")}</button>
          <button className="secondary" onClick={() => startEntry("OUT")}>− {t("cash.addOut")}</button>
          <button
            className="secondary"
            onClick={() => { setCountErr({}); setCountBalance(null); setCounting({ account: "CASH", counted: "", note: "", happened_on: today() }); }}
          >
            {t("cash.count")}
          </button>
        </div>
      )}

      <div className="toolbar" style={{ marginTop: 14, flexWrap: "wrap", gap: 8 }}>
        <select aria-label={t("cash.account")} value={account} onChange={(e) => setAccount(e.target.value)}>
          <option value="">{t("cash.allAccounts")}</option>
          {balance.accounts.map((a) => (
            <option key={a.account} value={a.account}>{a.label}</option>
          ))}
        </select>
        <input
          className="search" type="search" value={search}
          placeholder={t("cashBook.searchPh")} aria-label={t("common.search")}
          onChange={(e) => setSearch(e.target.value)}
        />
        <input
          type="number" inputMode="decimal" value={amountMin} style={{ width: 120 }}
          placeholder={t("cashBook.amountFrom")} aria-label={t("cashBook.amountFrom")}
          onChange={(e) => setAmountMin(e.target.value)}
        />
        <input
          type="number" inputMode="decimal" value={amountMax} style={{ width: 120 }}
          placeholder={t("cashBook.amountTo")} aria-label={t("cashBook.amountTo")}
          onChange={(e) => setAmountMax(e.target.value)}
        />
        <select aria-label={t("cashBook.reconciled")} value={reconciled} onChange={(e) => setReconciled(e.target.value)}>
          <option value="">{t("cashBook.reconAny")}</option>
          <option value="true">{t("cashBook.reconYes")}</option>
          <option value="false">{t("cashBook.reconNo")}</option>
        </select>
        <input
          type="date" value={day} aria-label={t("clients.filterDay")} title={t("clients.filterDay")}
          onChange={(e) => setDay(e.target.value)}
        />
        {filtered && <button type="button" className="secondary" onClick={resetFilters}>{t("common.resetFilters")}</button>}
        <button type="button" className="secondary" onClick={exportCsv}>{t("statements.csv")}</button>
        <button type="button" className={showDays ? "" : "secondary"} onClick={() => setShowDays((v) => !v)}>
          {t("cashBook.dayTotals")}
        </button>
      </div>

      {showDays && (
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">{t("cash.date")}</th>
                <th scope="col">{t("cash.in")}</th>
                <th scope="col">{t("cash.out")}</th>
                <th scope="col">{t("cashBook.dayNet")}</th>
                {balance.accounts.map((a) => <th scope="col" key={a.account}>{t("cashBook.closingOf", { account: a.label })}</th>)}
                <th scope="col">{t("cashBook.closingTotal")}</th>
              </tr>
            </thead>
            <tbody>
              {days.length === 0 ? (
                <tr><td colSpan={5 + balance.accounts.length} className="muted">{t("common.empty")}</td></tr>
              ) : days.map((d) => (
                <tr key={d.date}>
                  <td>{formatDate(d.date)}</td>
                  <td>{som(d.income)}</td>
                  <td>{som(d.outcome)}</td>
                  <td style={{ color: Number(d.net) < 0 ? "var(--danger-ink)" : undefined }}>{som(d.net)}</td>
                  {balance.accounts.map((a) => <td key={a.account}>{som(d.closing?.[a.account])}</td>)}
                  <td><strong>{som(d.closing_total)}</strong></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {rowsFailed && !rows.length ? (
        <LoadError onRetry={load} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          filtered={filtered}
          onReset={resetFilters}
        />
      )}
      <Pager page={page} count={count} pageSize={PAGE} onPage={setPage} />

      {/* --- Приход / расход руками --- */}
      {entry && (
        <Modal
          title={entry.kind === "IN" ? t("cash.addIn") : t("cash.addOut")}
          onClose={() => setEntry(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setEntry(null)}>{t("common.cancel")}</button>
              <button onClick={() => saveEntry()} disabled={busy}>{t("common.save")}</button>
            </>
          }
        >
          <div className="row">
            <Field className="grow" style={{ margin: 0 }} label={t("cash.account")}>
              <select value={entry.account} onChange={(e) => setEntry({ ...entry, account: e.target.value })}>
                {balance.accounts.map((a) => (
                  <option key={a.account} value={a.account}>{a.label}</option>
                ))}
              </select>
            </Field>
            <Field className="grow" style={{ margin: 0 }} label={t("cash.article")}>
              <select value={entry.article} onChange={(e) => setEntry({ ...entry, article: e.target.value })}>
                {MANUAL_ARTICLES[entry.kind].map((a) => (
                  <option key={a} value={a}>{t(`cash.article_${a}`)}</option>
                ))}
              </select>
            </Field>
          </div>
          {ARTICLE_HINTS.includes(entry.article) && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t(`cash.hint_${entry.article}`)}</p>
          )}
          {entry.kind === "OUT" && (
            <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>{t("cash.expensesViaFinance")}</p>
          )}
          {/* Расход «Прочее» уменьшает кассу, но в ОПиУ не попадает (G1-N1): если
              это аренда, расходники или зарплата — проводить надо тратой. */}
          {entry.kind === "OUT" && entry.article === "OTHER" && (
            <div className="callout" role="alert" style={{ marginTop: 8 }}>
              <p style={{ margin: "0 0 6px" }}>{t("cashBook.otherWarning")}</p>
              {!isAccountant && (
                <Link to="/admin/finance" className="btn-link" onClick={() => setEntry(null)}>
                  {t("cashBook.postAsExpense")} <Icon name="arrow-right" size={16} />
                </Link>
              )}
            </div>
          )}
          <div className="row">
            <Field className="grow" style={{ margin: 0 }} label={t("cash.amount")} required error={entryErr.amount}>
              <input
                type="number" step="any" inputMode="decimal" value={entry.amount} autoFocus
                onChange={(e) => setEntry({ ...entry, amount: e.target.value })}
              />
            </Field>
            <Field style={{ margin: 0, width: 180 }} label={t("cash.date")}>
              <input
                type="date" value={entry.happened_on}
                onChange={(e) => setEntry({ ...entry, happened_on: e.target.value })}
              />
            </Field>
          </div>
          <Field label={t("cash.note")}>
            <input
              value={entry.note}
              placeholder={t("cash.notePh")}
              onChange={(e) => setEntry({ ...entry, note: e.target.value })}
            />
          </Field>
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
          <Field label={t("cashBook.countDate")} hint={t("cashBook.countDateHint")}>
            <input type="date" value={counting.happened_on} max={today()}
              onChange={(e) => setCounting({ ...counting, happened_on: e.target.value })} />
          </Field>
          <div className="row">
            <Field className="grow" style={{ margin: 0 }} label={t("cash.account")}>
              <select value={counting.account} onChange={(e) => setCounting({ ...counting, account: e.target.value })}>
                {balance.accounts.map((a) => (
                  <option key={a.account} value={a.account}>{a.label}</option>
                ))}
              </select>
            </Field>
            <Field className="grow" style={{ margin: 0 }} label={t("cash.counted")} required error={countErr.counted}>
              <input
                type="number" step="any" inputMode="decimal" value={counting.counted} autoFocus
                onChange={(e) => setCounting({ ...counting, counted: e.target.value })}
              />
            </Field>
          </div>
          {counting.counted !== "" && (
            <div className="card" style={{ background: "var(--canvas)", padding: 12 }}>
              <div className="crow">
                <span className="k">{t("cash.bySystem")}</span>
                <span>{som((countBalance || balance).accounts.find((a) => a.account === counting.account)?.balance)}</span>
              </div>
              <div className="crow">
                <span className="k">{t("cash.diff")}</span>
                <strong style={{
                  color:
                    Number(counting.counted) -
                      Number((countBalance || balance).accounts.find((a) => a.account === counting.account)?.balance || 0) === 0
                      ? "var(--ok-ink)"
                      : "var(--danger-ink)",
                }}>
                  {som(
                    Number(counting.counted) -
                      Number((countBalance || balance).accounts.find((a) => a.account === counting.account)?.balance || 0)
                  )}
                </strong>
              </div>
            </div>
          )}
          <Field label={t("cash.note")}>
            <input value={counting.note} onChange={(e) => setCounting({ ...counting, note: e.target.value })} />
          </Field>
        </Modal>
      )}
    </>
  );
}
