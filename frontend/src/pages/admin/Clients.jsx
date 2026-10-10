import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import BulkPayModal from "../../components/BulkPayModal.jsx";
import ClientPicker from "../../components/ClientPicker.jsx";
import DataTable from "../../components/DataTable.jsx";
import Field, { focusFirstInvalid } from "../../components/Field.jsx";
import Icon from "../../components/Icon.jsx";
import LoadError from "../../components/LoadError.jsx";
import Modal from "../../components/Modal.jsx";
import Pager, { usePage } from "../../components/Pager.jsx";
import PrintAct from "../../components/PrintAct.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import { isCanceled, useLatest } from "../../utils/latest.js";
import { formatDate, formatMoney, formatNumber } from "../../utils/format.js";

// Сколько заказов показывать в карточке сразу — остальные под кнопкой.
const ORDERS_PREVIEW = 5;
// Оплат в карточке показываем столько же: история длиннее — в чеках.
const PAYMENTS_PREVIEW = 5;

// Период → параметры запроса. {mode: "all" | "month" | "day", year, month, day}.
function rangeParams(r) {
  if (r.mode === "day" && r.day) return { date_from: r.day, date_to: r.day };
  if (r.mode !== "month" || !r.month) return {};
  const last = new Date(r.year, r.month, 0).getDate();
  const mm = String(r.month).padStart(2, "0");
  return {
    date_from: `${r.year}-${mm}-01`,
    date_to: `${r.year}-${mm}-${String(last).padStart(2, "0")}`,
  };
}

const money = (n) => formatNumber(n);
const today = () => new Date().toLocaleDateString("sv-SE"); // YYYY-MM-DD, местная дата

// Соседний день для стрелок ‹ ›. Полдень — чтобы переход на летнее время не
// перекинул дату дважды.
function shiftDay(iso, delta) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + delta);
  return d.toLocaleDateString("sv-SE");
}

export default function Clients() {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const navigate = useNavigate();
  const { isAdmin, isAccountant } = useAuth();
  const canEdit = !isAccountant;
  const [clients, setClients] = useState([]);
  // Сколько клиентов всего по фильтру (ответ приходит постранично).
  const [count, setCount] = useState(0);
  const [listError, setListError] = useState(false);
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState(null);
  // Клиент, по которому открыт акт сверки.
  const [actFor, setActFor] = useState(null);
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
  // Ошибки формы нового клиента — рядом с полями: {phone, full_name, company_name}.
  const [createErr, setCreateErr] = useState({});
  // Общая выплата — одна сумма сразу за несколько заказов.
  const [payingClient, setPayingClient] = useState(null);
  // Склейка двойников: {from, preview} — что именно переедет, показываем до
  // подтверждения, потому что вторая карточка удаляется безвозвратно.
  const [merging, setMerging] = useState(null);
  // «Кому мы должны сдачу» — обратный список к должникам.
  const [onlyChange, setOnlyChange] = useState(false);
  // Период внутри карточки: за день, за месяц или за всё время. При открытии
  // берётся из фильтра списка, дальше переключается прямо в карточке — чтобы
  // посмотреть, что клиент брал сегодня, не закрывая её.
  const [cardRange, setCardRange] = useState({ mode: "all", year: new Date().getFullYear(), month: null, day: "" });
  // Стрелками месяца щёлкают быстро: ответ на старый запрос не должен
  // перетереть карточку за месяц, выбранный позже.
  const detailReq = useRef(0);
  // Поиск шлёт запрос на каждое нажатие; побеждает последний.
  const nextList = useLatest();

  // Фильтры и сортировка одной строкой: смена любого сбрасывает страницу на
  // первую, а устаревший ответ не перетирает свежий.
  const filterKey = JSON.stringify([search, period.year, period.month, day, onlyDebt, onlyChange, minOrders, sort]);
  const [page, setPage] = usePage(filterKey);
  const filtered = !!(search || day || period.month || onlyDebt || onlyChange || minOrders);

  // День важнее месяца: выбран день — смотрим ровно его, иначе весь месяц.
  function listRange() {
    if (day) return { mode: "day", year: period.year, month: period.month, day };
    return { mode: period.month ? "month" : "all", year: period.year, month: period.month, day: "" };
  }

  const periodParams = () => rangeParams(listRange());

  function resetFilters() {
    setSearch("");
    setDay("");
    setPeriod((p) => ({ ...p, month: null }));
    setOnlyDebt(false);
    setOnlyChange(false);
    setMinOrders("");
  }

  function load() {
    const params = {
      ...(search ? { search } : {}),
      ...periodParams(),
      ...(onlyDebt ? { has_debt: 1 } : {}),
      ...(onlyChange ? { has_change: 1 } : {}),
      ...(Number(minOrders) > 0 ? { min_orders: Number(minOrders) } : {}),
      ordering: (sort.dir === "desc" ? "-" : "") + sort.key,
      ...(page > 1 ? { page } : {}),
    };
    api
      .get("/clients/clients/", { params, signal: nextList() })
      .then((r) => {
        setClients(r.data.results);
        setCount(r.data.count ?? r.data.results.length);
        setListError(false);
      })
      .catch((e) => {
        if (isCanceled(e)) return;
        // Страница, которой больше нет (клиентов стало меньше) — на первую.
        if (e.response?.status === 404 && page > 1) return setPage(1);
        setListError(true);
        toast(apiError(e, t("common.loadFailed")), "error");
      });
  }

  useEffect(() => {
    const id = setTimeout(load, 250);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, page]);

  function onSort(key) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }));
  }

  // Карточка всегда грузится за период, выбранный в ней самой: после выдачи
  // пароля или смены реферера фильтр «сегодня» не должен слетать на всю историю.
  async function fetchDetail(id, range = cardRange) {
    const req = ++detailReq.current;
    const { data } = await api.get(`/clients/clients/${id}/`, { params: rangeParams(range) });
    if (req === detailReq.current) setDetail(data);
  }

  async function openDetail(c) {
    const range = listRange();
    setCardRange(range);
    try {
      await fetchDetail(c.id, range);
    } catch (e) {
      // Без этого неудавшееся открытие карточки было «кнопка не нажимается».
      return toast(apiError(e, t("common.error")), "error");
    }
    setShowAllOrders(false);
    setShowAllPayments(false);
    setMerging(null);
  }

  async function changeCardRange(next) {
    setCardRange(next);
    setShowAllOrders(false);
    setShowAllPayments(false);
    try {
      await fetchDetail(detail.id, next);
    } catch (e) {
      // Не оставляем на экране «17.09» над заказами прошлого периода.
      setCardRange(cardRange);
      toast(apiError(e, t("common.error")), "error");
    }
  }

  // Переключатель «День / Месяц / Весь период». День и месяц не сбрасывают
  // друг друга: из дня 12.09 в «Месяц» попадаем в сентябрь, а не в текущий.
  function pickCardMode(mode) {
    if (mode === cardRange.mode) return;
    const now = new Date();
    if (mode === "day") {
      return changeCardRange({ ...cardRange, mode, day: cardRange.day || today() });
    }
    if (mode === "month") {
      const [y, m] = (cardRange.day || "").split("-").map(Number);
      return changeCardRange({
        ...cardRange,
        mode,
        year: cardRange.month ? cardRange.year : y || now.getFullYear(),
        month: cardRange.month || m || now.getMonth() + 1,
      });
    }
    changeCardRange({ ...cardRange, mode: "all" });
  }

  // Акт сверки считает сальдо от нуля, поэтому ему нужна вся история, а не
  // заказы за день, выбранный в карточке, — иначе итог акта не сойдётся с долгом.
  async function openAct() {
    if (cardRange.mode === "all") return setActFor(detail);
    try {
      const { data } = await api.get(`/clients/clients/${detail.id}/`);
      setActFor(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  // После выплаты перечитываем карточку и список: изменились и долги заказов,
  // и колонка «Долг» в таблице.
  async function refreshDetail(id) {
    try {
      await fetchDetail(id);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
    load();
  }

  // Правка поля карточки (ФИО, компания, ИНН) — по уходу из поля. Если сервер
  // отказал (например, стёрли ФИО у физлица), возвращаем в поле старое значение:
  // иначе на экране осталась бы пустота, которой в базе нет.
  async function saveField(field, input) {
    const next = input.value.trim();
    const prev = detail[field] || "";
    if (next === prev) return;
    try {
      await api.patch(`/clients/clients/${detail.id}/`, { [field]: next });
      await refreshDetail(detail.id);
      toast(t("common.saved"));
    } catch (e) {
      input.value = prev;
      toast(apiError(e, t("common.error")), "error");
    }
  }

  // Постоянная скидка клиента, % (CLI-02): касса подставляет её сама. Задаёт
  // только админ; число сравниваем как число — с сервера приходит «5.00».
  async function saveDiscount(input) {
    const raw = input.value.trim();
    const prev = Number(detail.discount_percent || 0);
    const next = raw === "" ? 0 : Number(raw);
    if (Number.isNaN(next) || next < 0 || next > 100) {
      input.value = String(prev);
      return toast(t("clients.discountBad"), "error");
    }
    if (next === prev) return;
    try {
      await api.patch(`/clients/clients/${detail.id}/`, { discount_percent: next });
      await refreshDetail(detail.id);
      toast(t("common.saved"));
    } catch (e) {
      input.value = String(prev);
      toast(apiError(e, t("common.error")), "error");
    }
  }

  // Пароль кабинета выдаёт админ и диктует клиенту. Показывается один раз:
  // в базе только хеш, посмотреть повторно нельзя — можно выдать новый.
  async function issuePassword() {
    if (detail.has_password && !(await confirm(t("clients.reissuePassConfirm")))) return;
    try {
      const { data } = await api.post(`/clients/clients/${detail.id}/set-password/`, {});
      setIssuedPassword(data.password);
      await fetchDetail(detail.id);
    } catch {
      toast(t("common.error"), "error");
    }
  }

  const errMsg = (e) => apiError(e, t("common.error"));

  async function createClient() {
    const body = { ...creating };
    if (body.discount_percent === "" || body.discount_percent == null) delete body.discount_percent;
    // Ошибки — рядом с полем, и фокус на первом неверном: тост на три секунды
    // не говорил, ЧТО именно заполнено не так.
    const errs = {};
    if (!body.phone?.trim()) errs.phone = t("clients.needPhone");
    if (body.type === "OSOO") {
      if (!body.company_name?.trim()) errs.company_name = t("checkout.needCompany");
    } else if (!body.full_name?.trim()) errs.full_name = t("checkout.needName");
    setCreateErr(errs);
    if (Object.keys(errs).length) return focusFirstInvalid();
    try {
      await api.post("/clients/clients/", body);
      setCreating(null);
      setCreateErr({});
      load();
      toast(t("clients.created"));
    } catch (e) {
      // 400 с полями ({"phone": ["Этот номер уже записан…"]}) — к своим полям.
      const data = e.response?.data;
      if (e.response?.status === 400 && data && typeof data === "object") {
        const fieldMsgs = {};
        ["phone", "full_name", "company_name", "inn", "discount_percent"].forEach((k) => {
          if (data[k]) fieldMsgs[k] = [].concat(data[k]).join(" ");
        });
        if (Object.keys(fieldMsgs).length) {
          setCreateErr(fieldMsgs);
          return focusFirstInvalid();
        }
      }
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
      await fetchDetail(data.id);
      load();
      toast(t("common.save"));
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
          <span style={{ color: "var(--danger-ink)", fontWeight: 600 }}>
            {formatMoney(c.debt)}
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
          <span style={{ color: "var(--accent-ink)", fontWeight: 600 }}>
            {formatMoney(c.change_due)}
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
        <button
          className="ghost"
          onClick={(e) => { e.stopPropagation(); openDetail(c); }}
          aria-label={`${t("common.edit")}: ${c.display_name}`}
        >
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
          type="search"
          aria-label={t("common.search")}
          placeholder={`${t("common.search")} (${t("clients.searchHint")})`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          type="button"
          onClick={() => { setCreateErr({}); setCreating({ type: "PHYSICAL", full_name: "", company_name: "", phone: "" }); }}
        >
          + {t("clients.newClient")}
        </button>
      </div>

      {/* Период: месяц стрелками или конкретный день. Показываем клиентов,
          которые заказывали в это время; «Заказов» тогда — за этот же период. */}
      <div className="toolbar" style={{ alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
        <MonthPicker value={period} onChange={(v) => { setPeriod(v); setDay(""); }} />
        <Field style={{ margin: 0 }} label={t("clients.filterDay")}>
          <input type="date" value={day} onChange={(e) => setDay(e.target.value)} />
        </Field>
        <Field style={{ margin: 0, width: 130 }} label={t("clients.minOrders")}>
          <input
            type="number"
            min="0"
            inputMode="numeric"
            value={minOrders}
            onChange={(e) => setMinOrders(e.target.value)}
            placeholder="0"
          />
        </Field>
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
          <button className="ghost" onClick={resetFilters}>
            {t("common.reset")}
          </button>
        )}
      </div>
      {(day || period.month) && (
        <p className="muted" style={{ fontSize: 13, marginTop: -4 }}>{t("clients.periodHint")}</p>
      )}

      {listError && !clients.length ? (
        <LoadError onRetry={load} />
      ) : (
        <DataTable
          columns={columns}
          rows={clients}
          sort={sort}
          onSort={onSort}
          onRowClick={openDetail}
          filtered={filtered}
          onReset={resetFilters}
        />
      )}
      <Pager page={page} count={count} onPage={setPage} />

      {detail && (
        <Modal title={detail.display_name} onClose={() => setDetail(null)}>
          {/* Имя правится прямо в карточке: на кассе его набирают на ходу, с
              опечатками. Бухгалтер карточки не правит — у него только чтение.
              key по клиенту: иначе при переходе к другому клиенту неуправляемое
              поле оставило бы в себе имя предыдущего. */}
          {detail.type === "OSOO" && (
            <div className="crow">
              <span className="k">{t("clients.companyName")}</span>
              {canEdit ? (
                <input
                  key={`company-${detail.id}`}
                  aria-label={t("clients.companyName")}
                  defaultValue={detail.company_name || ""}
                  style={{ width: 240, height: 34, textAlign: "right" }}
                  onBlur={(e) => saveField("company_name", e.target)}
                />
              ) : (
                <span>{detail.company_name || "—"}</span>
              )}
            </div>
          )}
          <div className="crow">
            <span className="k">{t("clients.fullName")}</span>
            {canEdit ? (
              <input
                key={`name-${detail.id}`}
                aria-label={t("clients.fullName")}
                defaultValue={detail.full_name || ""}
                style={{ width: 240, height: 34, textAlign: "right" }}
                onBlur={(e) => saveField("full_name", e.target)}
              />
            ) : (
              <span>{detail.full_name || "—"}</span>
            )}
          </div>
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
                  key={`inn-${detail.id}`}
                  aria-label={t("clients.inn")}
                  defaultValue={detail.inn || ""}
                  placeholder={t("clients.innPh")}
                  style={{ width: 200, height: 34, textAlign: "right" }}
                  onBlur={(e) => saveField("inn", e.target)}
                />
              ) : (
                <span>{detail.inn || "—"}</span>
              )}
            </div>
          )}
          <div className="crow">
            <span className="k">{t("clients.discount")}</span>
            {isAdmin ? (
              <input
                key={`disc-${detail.id}-${detail.discount_percent}`}
                type="number"
                inputMode="decimal"
                min="0"
                max="100"
                aria-label={t("clients.discount")}
                title={t("clients.discountHint")}
                defaultValue={String(+Number(detail.discount_percent || 0))}
                style={{ width: 120, height: 34, textAlign: "right" }}
                onBlur={(e) => saveDiscount(e.target)}
              />
            ) : (
              <span>{Number(detail.discount_percent) > 0 ? `${formatNumber(detail.discount_percent, { max: 2 })} %` : "—"}</span>
            )}
          </div>
          <div className="crow">
            <span className="k">{t("clients.telegram")}</span>
            <span>{detail.is_telegram_linked ? t("clients.linked") : t("clients.notLinked")}</span>
          </div>
          <div className="crow">
            <span className="k">{t("clients.orders")}</span>
            <span>
              {detail.stats?.orders_count}
              {detail.stats?.cancelled_count > 0 && (
                <span className="muted"> · {t("clients.ordersCancelled", { n: detail.stats.cancelled_count })}</span>
              )}
            </span>
          </div>
          <div className="crow">
            <span className="k">{t("clients.ltv")}</span>
            <span><strong>{formatMoney(detail.stats?.lifetime_value || 0)}</strong></span>
          </div>
          <div className="crow">
            <span className="k">{t("receipts.debt")}</span>
            <span className="row" style={{ gap: 8, alignItems: "center", margin: 0 }}>
              {Number(detail.debt) > 0 ? (
                <strong style={{ color: "var(--danger-ink)" }}>{formatMoney(detail.debt)}</strong>
              ) : (
                <span className="paid">{formatMoney(0)}</span>
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
              <strong style={{ color: "var(--accent-ink)" }}>
                {formatMoney(detail.change_due)}
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
                  style={{ padding: "3px 8px", height: "auto", fontSize: 12, color: "var(--accent-ink)" }}
                  onClick={issuePassword}
                >
                  {detail.has_password ? t("clients.reissuePass") : t("clients.issuePass")}
                </button>
              )}
            </span>
          </div>

          {/* Заказы клиента — что покупал. Период переключается прямо здесь:
              «что он брал сегодня» и «на сколько набрал за месяц» — вопросы у
              стойки, ради них не стоит закрывать карточку и крутить фильтр
              списка. Оплаты ниже режутся тем же периодом (по дате оплаты). */}
          <div className="field" style={{ marginTop: 14 }}>
            <label>{t("clients.ordersList")}</label>
            <div className="row" style={{ margin: "0 0 8px", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              {["day", "month", "all"].map((m) => (
                <button
                  key={m}
                  type="button"
                  className={cardRange.mode === m ? "" : "secondary"}
                  style={{ padding: "4px 12px", height: "auto", fontSize: 13 }}
                  onClick={() => pickCardMode(m)}
                >
                  {t(`clients.range_${m}`)}
                </button>
              ))}
            </div>
            {cardRange.mode === "month" && (
              <div style={{ marginBottom: 8 }}>
                <MonthPicker
                  label={false}
                  value={cardRange}
                  // «Все месяцы» в выпадашке — то же, что кнопка «Весь период».
                  onChange={(v) => changeCardRange({ ...cardRange, ...v, mode: v.month ? "month" : "all" })}
                />
              </div>
            )}
            {cardRange.mode === "day" && (
              <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
                <button
                  className="ghost"
                  onClick={() => changeCardRange({ ...cardRange, day: shiftDay(cardRange.day, -1) })}
                  aria-label={t("clients.prevDay")}
                >
                  ‹
                </button>
                <input
                  type="date"
                  value={cardRange.day}
                  max={today()}
                  style={{ width: 170 }}
                  onChange={(e) => e.target.value && changeCardRange({ ...cardRange, day: e.target.value })}
                />
                <button
                  className="ghost"
                  disabled={cardRange.day >= today()}
                  onClick={() => changeCardRange({ ...cardRange, day: shiftDay(cardRange.day, 1) })}
                  aria-label={t("clients.nextDay")}
                >
                  ›
                </button>
              </div>
            )}
            {/* Итог за выбранный период. Шапка карточки («Заказов», «Сумма
                покупок») — за всё время, а тут видно, сколько он набрал именно
                за этот день или месяц. Долг — по заказам периода. */}
            {cardRange.mode !== "all" && detail.orders?.length > 0 && (() => {
              const live = detail.orders.filter((o) => o.status !== "CANCELLED").length;
              const sum = detail.orders.reduce((s, o) => s + Number(o.total_price) - Number(o.refunded_amount || 0), 0);
              const debt = detail.orders.reduce((s, o) => s + Number(o.debt || 0), 0);
              return (
                <div
                  className="crow"
                  style={{ background: "var(--primary-soft)", borderRadius: "var(--r-md)", padding: "8px 12px", marginBottom: 6 }}
                >
                  <span>{t("clients.periodOrders", { n: live })}</span>
                  <span>
                    <strong>{formatMoney(sum)}</strong>
                    {debt > 0 && (
                      <span style={{ color: "var(--danger-ink)", fontSize: 13 }}>
                        {" · "}{t("receipts.debt")}: {money(debt)}
                      </span>
                    )}
                  </span>
                </div>
              );
            })()}
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
                    <span className="muted" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      {formatDate(o.created_at)}
                      {/* «Ещё раз то же самое» — самый частый разговор у стойки.
                          Отсюда до кассы один клик, состав уже собран. */}
                      <button
                        type="button"
                        className="ghost"
                        style={{ padding: "3px 8px", height: "auto", fontSize: 12, color: "var(--accent-ink)" }}
                        onClick={() => navigate(`${isAdmin ? "/admin" : "/app/checkout"}?repeat=${o.id}`)}
                        title={t("receipts.repeatHint")}
                      >
                        {t("receipts.repeat")}
                      </button>
                    </span>
                  </div>
                  {/* Возвращённые строки остаются в истории — зачёркнутыми и с
                      пометкой; раньше они просто исчезали, и у возвращённого
                      заказа оставался голый итог без объяснений. */}
                  {o.items.map((it, i) => (
                    <div className="crow" key={i} style={{ fontSize: 13 }}>
                      <span className="k" style={it.is_returned ? { textDecoration: "line-through" } : undefined}>
                        {it.title} × {Number(it.quantity)}
                        {it.is_returned && (
                          <span className="badge warn" style={{ marginLeft: 6, textDecoration: "none" }}>
                            {t("receipts.returned")}
                          </span>
                        )}
                      </span>
                      <span style={it.is_returned ? { textDecoration: "line-through", color: "var(--ink-muted)" } : undefined}>
                        {formatMoney(it.line_total)}
                      </span>
                    </div>
                  ))}
                  <div className="crow" style={{ borderTop: "1px solid var(--hairline)", marginTop: 4, paddingTop: 4 }}>
                    <strong>{formatMoney(o.total_price)}</strong>
                    {Number(o.refunded_amount) > 0 && (
                      <span className="muted" style={{ fontSize: 13 }}>
                        {t("clients.orderRefunded", { sum: formatNumber(o.refunded_amount) })}
                      </span>
                    )}
                    {Number(o.debt) > 0 && (
                      <span style={{ color: "var(--danger-ink)", fontSize: 13 }}>
                        {t("receipts.debt")}: {formatNumber(o.debt)}
                      </span>
                    )}
                    {Number(o.change_due) > 0 && (
                      <span style={{ color: "var(--accent-ink)", fontSize: 13 }}>
                        {t("receipts.change")}: {formatNumber(o.change_due)}
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
                style={{ color: "var(--accent-ink)" }}
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
                    <span className="muted">{formatDate(p.paid_on)}</span>
                    {" · "}
                    №{p.order_number}
                    {p.order_title ? <span className="muted"> · {p.order_title}</span> : null}
                  </span>
                  <span>
                    <strong>{formatMoney(p.amount)}</strong>
                    <span className="muted" style={{ fontSize: 12 }}> · {p.method_display}</span>
                  </span>
                </div>
              ))}
              {detail.payments.length > PAYMENTS_PREVIEW && (
                <button
                  className="ghost"
                  style={{ color: "var(--accent-ink)" }}
                  onClick={() => setShowAllPayments((v) => !v)}
                >
                  {showAllPayments
                    ? t("clients.ordersCollapse")
                    : t("clients.paymentsShowAll", { count: detail.payments.length })}
                </button>
              )}
            </div>
          )}

          {/* Кто привёл клиента. Поставить можно один раз; сменить уже
              поставленного — только админ, прямо здесь. */}
          <div className="field" style={{ marginTop: 14 }}>
            <label htmlFor="referrer-picker">{t("clients.referredByLabel")}</label>
            {/* Выбор — через поиск по серверу (ClientPicker), а не <select> с
                первыми 25 клиентами: при поиске в нём оставалась единственная
                опция «— никто —», и администратор мог затереть настоящего
                реферера, не заметив. Не предлагаем самого клиента и тех, кого
                он привёл (получилось бы кольцо); остальные звенья цепочки
                проверяет сервер. */}
            {!detail.referred_by || isAdmin ? (
              <ClientPicker
                id="referrer-picker"
                value={detail.referred_by || ""}
                valueLabel={detail.referred_by ? detail.referred_by_name : undefined}
                noneLabel={t("clients.noReferrer")}
                excludeIds={[detail.id, ...(detail.referrals?.list || []).map((r) => r.id)]}
                onChange={(id) => id !== (detail.referred_by || "") && setReferrer(id)}
              />
            ) : (
              // Складовщик → реферер зафиксирован; сменить его может админ в
              // этой же карточке. Очереди заявок больше нет (27.09).
              <div className="crow" style={{ padding: "8px 0" }}>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <Icon name="lock" size={15} /> {detail.referred_by_name}
                </span>
                <span className="muted" style={{ fontSize: 12 }}>{t("clients.referralLocked")}</span>
              </div>
            )}
          </div>

          {/* Clients this one referred */}
          <div className="field" style={{ margin: 0 }}>
            <label>
              {t("clients.referrals")}: {detail.referrals?.count || 0}
              {detail.referrals?.count > 0 && (
                <span className="muted"> · {formatMoney(detail.referrals.total_value)}</span>
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
                <strong style={{ color: "var(--accent-ink)" }}>{t("clients.referralBonus")}</strong>
                <strong style={{ color: "var(--accent-ink)" }}>
                  {formatMoney(detail.referrals.bonus)}
                </strong>
              </div>
            )}
            {detail.referrals?.list?.length ? (
              detail.referrals.list.map((r) => (
                <div className="crow" key={r.id}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Icon name="user" size={15} /> {r.display_name}
                  </span>
                  <span className="muted">{formatMoney(r.lifetime_value)}</span>
                </div>
              ))
            ) : (
              <span className="muted">{t("common.empty")}</span>
            )}
          </div>

          {/* Акт сверки — тем же документом, что и в 1С, закрывают спор о долге
              с юрлицом. Данные уже в карточке, форма собирается из них. */}
          <div className="row" style={{ marginTop: 16 }}>
            <button className="secondary" onClick={openAct}>
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
              <ClientPicker
                value={merging?.from || ""}
                noneLabel={t("clients.mergePick")}
                excludeIds={[detail.id]}
                onChange={(id) => previewMerge(id)}
                aria-label={t("clients.mergeTitle")}
              />

              {merging?.preview && (
                <div className="card" style={{ background: "var(--canvas)", padding: 12, marginTop: 8 }}>
                  <div className="crow">
                    <span className="k">{t("clients.mergeOrders")}</span>
                    <strong>{merging.preview.orders}</strong>
                  </div>
                  {Number(merging.preview.debt) > 0 && (
                    <div className="crow">
                      <span className="k">{t("receipts.debt")}</span>
                      <strong style={{ color: "var(--danger-ink)" }}>
                        {formatMoney(merging.preview.debt)}
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
          onClose={() => { setCreating(null); setCreateErr({}); }}
          footer={
            <>
              <button className="secondary" onClick={() => { setCreating(null); setCreateErr({}); }}>{t("common.cancel")}</button>
              <button onClick={createClient}>{t("common.add")}</button>
            </>
          }
        >
          <Field label={t("clients.type")}>
            <select
              value={creating.type}
              onChange={(e) => {
                setCreating({ ...creating, type: e.target.value });
                setCreateErr({});
              }}
            >
              <option value="PHYSICAL">{t("clients.physical")}</option>
              <option value="OSOO">{t("clients.osoo")}</option>
            </select>
          </Field>
          {/* ФИО и компания — оба поля, а не «или-или».
              Раньше форма показывала одно вместо другого: у ОсОО нельзя было
              записать контактное лицо (кому звонить по заказу), а у физлица —
              компанию, от которой он заказывает. При этом в базе есть оба поля
              и заполнены оба — форма просто не давала их ввести. */}
          <Field
            label={t("clients.fullName")}
            required={creating.type !== "OSOO"}
            error={createErr.full_name}
          >
            <input
              value={creating.full_name}
              onChange={(e) => setCreating({ ...creating, full_name: e.target.value })}
              placeholder={creating.type === "OSOO" ? t("clients.contactPh") : ""}
              autoComplete="name"
              autoFocus
            />
          </Field>
          <Field
            label={t("clients.companyName")}
            required={creating.type === "OSOO"}
            optional={creating.type !== "OSOO"}
            optionalLabel={t("common.optional")}
            error={createErr.company_name}
          >
            <input
              value={creating.company_name}
              onChange={(e) => setCreating({ ...creating, company_name: e.target.value })}
              placeholder={t("clients.companyPh")}
              autoComplete="organization"
            />
          </Field>
          <Field label={t("clients.phone")} required error={createErr.phone}>
            <input
              type="tel"
              value={creating.phone}
              onChange={(e) => setCreating({ ...creating, phone: e.target.value })}
              placeholder="+996…"
              inputMode="tel"
              autoComplete="tel"
            />
          </Field>
          {/* ИНН спрашиваем только у юрлица и только ради счёта на оплату: без
              него бухгалтерия клиента счёт не проведёт. У физлица его нет. */}
          {creating.type === "OSOO" && (
            <Field label={t("clients.inn")} error={createErr.inn}>
              <input
                value={creating.inn ?? ""}
                onChange={(e) => setCreating({ ...creating, inn: e.target.value })}
                placeholder={t("clients.innPh")}
              />
            </Field>
          )}
          {isAdmin && (
            <Field label={t("clients.discount")} error={createErr.discount_percent} hint={t("clients.discountHint")}>
              <input
                type="number"
                inputMode="decimal"
                min="0"
                max="100"
                value={creating.discount_percent ?? ""}
                onChange={(e) => setCreating({ ...creating, discount_percent: e.target.value })}
                placeholder="0"
              />
            </Field>
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
