import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import api from "../../api/api.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import DailyProfitChart from "../../components/DailyProfitChart.jsx";
import DataTable from "../../components/DataTable.jsx";
import ExpenseKindFormModal from "../../components/ExpenseKindFormModal.jsx";
import ExpenseKindModal from "../../components/ExpenseKindModal.jsx";
import ExpenseListSection from "../../components/ExpenseListSection.jsx";
import Icon from "../../components/Icon.jsx";
import MonthPicker from "../../components/MonthPicker.jsx";
import { useUI } from "../../components/UIProvider.jsx";

const som = (n) => `${Math.round(Number(n) || 0).toLocaleString("ru-RU")} сом`;
const q2 = (n) => Number(n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 });

// Границы выбранного месяца. month = null → весь период (без дат).
function periodParams({ year, month }) {
  if (!month) return null;
  const last = new Date(year, month, 0).getDate();
  const p = (n) => String(n).padStart(2, "0");
  return { date_from: `${year}-${p(month)}-01`, date_to: `${year}-${p(month)}-${p(last)}` };
}

function Stat({ label, value, color, sub }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value" style={color ? { color: `var(--${color})` } : undefined}>
        {value}
      </div>
      {/* Вторая строка — объём работы под суммой: 12 000 сом это много мелких
          резов или один большой лист, по одной сумме не видно. */}
      {sub ? <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>{sub}</div> : null}
    </div>
  );
}

export default function Finance() {
  const { t } = useTranslation();
  // Бухгалтер: сервер не примет от него ни одной записи в финансах, поэтому и
  // показывать ему кнопки записи нельзя — см. комментарий у `editRow`.
  const { isAccountant: readOnly } = useAuth();
  const { toast } = useUI();
  const now = new Date();
  // Отчёт заказчика — месячный, поэтому по умолчанию открываем текущий месяц.
  // Раньше блоки считались за всё время, а списки трат фильтровались отдельно —
  // цифры сверху и снизу жили в разных периодах.
  const [period, setPeriod] = useState({ year: now.getFullYear(), month: now.getMonth() + 1 });
  // Открываемся на месяце ПОСЛЕДНЕГО чека, а не на календарном текущем.
  // Первого числа (и вообще в любой месяц, где ещё не продавали) отчёт
  // встречал нулями во всех строках, и это читается как «система пустая»,
  // хотя данные есть — просто в прошлом месяце.
  useEffect(() => {
    api
      .get("/sales/receipts/", { params: { ordering: "-created_at", page_size: 1 } })
      .then((r) => {
        const last = (r.data.results || [])[0];
        if (!last) return;
        const d = new Date(last.created_at);
        const y = d.getFullYear();
        const m = d.getMonth() + 1;
        if (y !== now.getFullYear() || m !== now.getMonth() + 1) setPeriod({ year: y, month: m });
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [report, setReport] = useState(null);
  const reportFor = useRef("");   // какой месяц ждём — чтобы не осел ответ устаревшего запроса
  const [settings, setSettings] = useState(null);
  // Закрытие периода: до какой даты всё «на замке».
  const [lock, setLock] = useState(null);
  const [lockDraft, setLockDraft] = useState("");
  // Открытые диалоги: записи вида и настройка вида.
  const [openKind, setOpenKind] = useState(null);
  const [editKind, setEditKind] = useState(null); // {kind} | {block} для нового
  // Счётчик правок: по нему перезагружается график по дням, чтобы он не спорил
  // со сводкой после добавления траты.
  const [revision, setRevision] = useState(0);

  const [matReport, setMatReport] = useState([]);
  const [matFilter, setMatFilter] = useState("");
  const [matTotals, setMatTotals] = useState(null);
  // Конкретный день в дополнение к месяцу — для сверки «что продали 14-го».
  const [matDay, setMatDay] = useState("");
  // Справочник видов расхода: по нему группируются списки трат внизу.
  const [kinds, setKinds] = useState([]);

  const params = periodParams(period);
  const matParams = matDay ? { date_from: matDay, date_to: matDay } : params || {};

  function loadReport() {
    // Отчёт за месяц запрашивается дважды подряд при первой загрузке: сразу за
    // календарный месяц, а следом за месяц последнего чека. Ответы возвращаются
    // не в том порядке, в каком ушли, и без этой отметки на экране оседал отчёт
    // первого запроса — селектор показывал один месяц, цифры другой.
    const wanted = `${period.year}-${period.month}`;
    reportFor.current = wanted;
    api
      .get("/finance/report/", { params: params || {} })
      .then((r) => {
        if (reportFor.current === wanted) setReport(r.data);
      })
      .catch(() => toast(t("common.error"), "error"));
  }
  // Траты поменяли: перечитываем отчёт и толкаем график по дням.
  function reloadAll() {
    loadReport();
    setRevision((n) => n + 1);
  }
  useEffect(() => {
    api.get("/finance/settings/").then((r) => setSettings(r.data));
    api.get("/finance/period/").then((r) => { setLock(r.data); setLockDraft(r.data.closed_through || ""); }).catch(() => setLock({}));
    api
      .get("/finance/expense-kinds/")
      .then((r) => setKinds(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(loadReport, [period.year, period.month]);

  function loadMaterialReport() {
    api
      .get("/finance/material-report/", { params: matParams })
      .then((r) => {
        setMatReport(r.data.rows);
        setMatTotals(r.data.totals || null);
      })
      .catch(() => toast(t("common.error"), "error"));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(loadMaterialReport, [period.year, period.month, matDay]);

  // Закрыть период — обычное действие месяца; открыть обратно тоже можно, но
  // это осознанный шаг, и оба попадают в журнал действий.
  function saveLock(value) {
    api
      .patch("/finance/period/", { closed_through: value || null })
      .then((r) => {
        setLock(r.data);
        setLockDraft(r.data.closed_through || "");
        toast(value ? t("period.closed", { date: new Date(value).toLocaleDateString("ru-RU") }) : t("period.opened"));
      })
      .catch((e) => toast(e.response?.data?.detail || t("common.error"), "error"));
  }

  function saveField(field, value) {
    const payload = value === "" ? 0 : Number(value);
    api
      .patch("/finance/settings/", { [field]: payload })
      .then(loadReport)
      .catch(() => toast(t("common.error"), "error"));
  }

  const filteredMat = matFilter
    ? matReport.filter((r) => String(r.id) === matFilter)
    : matReport;

  function downloadCsv() {
    const head = [
      t("common.name"), t("warehouse.type"), t("finance.colOrders"),
      t("finance.colSoldArea"), t("finance.colSoldSheets"),
      t("finance.colMatSum"), t("finance.colCutSum"), t("finance.colReceived"),
      t("finance.colStock"),
    ];
    const lines = [head.join(";")];
    for (const r of filteredMat) {
      lines.push([
        r.name,
        r.type || "",
        r.orders,
        Number(r.sold_area || 0).toFixed(2),
        Number(r.sold_sheets || 0).toFixed(2),
        Math.round(Number(r.material_revenue || 0)),
        Math.round(Number(r.cut_revenue || 0)),
        Number(r.received || 0).toFixed(2),
        Number(r.stock || 0).toFixed(2),
      ].join(";"));
    }
    if (matTotals && !matFilter) {
      lines.push([
        t("finance.totalRow"), "", matTotals.orders,
        Number(matTotals.sold_area || 0).toFixed(2),
        Number(matTotals.sold_sheets || 0).toFixed(2),
        Math.round(Number(matTotals.material_revenue || 0)),
        Math.round(Number(matTotals.cut_revenue || 0)),
        Number(matTotals.received || 0).toFixed(2),
        "",
      ].join(";"));
    }
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "rezka-po-materialam.csv";
    a.click();
  }

  // Складской лист «как в Excel» живёт на Складе, отдельной вкладкой. Здесь —
  // прежняя таблица продаж и резки по материалам.
  const matColumns = [
    { key: "name", label: t("common.name"), render: (r) => <strong>{r.name}</strong> },
    { key: "type", label: t("warehouse.type"), render: (r) => (r.type ? <span className="chip">{r.type}</span> : "—") },
    { key: "orders", label: t("finance.colOrders") },
    { key: "sold_area", label: t("finance.colSoldArea"), render: (r) => q2(r.sold_area) },
    { key: "sold_sheets", label: t("finance.colSoldSheets"), render: (r) => q2(r.sold_sheets) },
    { key: "material_revenue", label: t("finance.colMatSum"), render: (r) => som(r.material_revenue) },
    { key: "cut_revenue", label: t("finance.colCutSum"), render: (r) => som(r.cut_revenue) },
    { key: "received", label: t("finance.colReceived"), render: (r) => q2(r.received) },
    { key: "stock", label: t("finance.colStock"), render: (r) => `${q2(r.stock)} ${t(`unit.${r.unit}`)}` },
  ];

  // Группы для списков трат внизу — как раньше были отдельные разделы.
  const fixedKinds = kinds.filter((k) => k.block === "FIXED" && k.code !== "SALARY");
  const salaryKinds = kinds.filter((k) => k.code === "SALARY");
  const purchaseKinds = kinds.filter((k) => k.block === "VARIABLE" || k.block === "MATERIALS");

  if (!report || !settings || !lock) return <p className="muted">{t("common.loading")}</p>;

  // Бухгалтер — проверяющий: видит всё, не трогает ничего. Раньше ему
  // показывали полный админский набор — «Закрыть период», диалог с кнопкой
  // «Добавить», настройку видов, — и каждое нажатие возвращало 403 или
  // невнятное «Произошла ошибка». Кнопки, которая гарантированно откажет,
  // быть не должно: в «Чеках» это уже так, теперь и здесь.
  const editRow = (label, field) =>
    readOnly ? (
      <div className="crow" key={field}>
        <span className="k">{label}</span>
        <strong>{som(settings[field] ?? 0)}</strong>
      </div>
    ) : (
      <div className="crow" key={field}>
        <span className="k">{label}</span>
        <input
          type="number"
          value={settings[field] ?? ""}
          onChange={(e) => setSettings({ ...settings, [field]: e.target.value })}
          onBlur={(e) => saveField(field, e.target.value)}
          placeholder="0"
          style={{ width: 150, height: 34, textAlign: "right" }}
        />
      </div>
    );

  // Заголовок блока и подытог — визуальное разделение как в Excel заказчика.
  const blockHead = (label) => (
    <div
      style={{
        background: "var(--primary-soft)",
        borderRadius: "var(--r-md)",
        padding: "8px 14px",
        margin: "0 0 6px",
        fontWeight: 700,
        color: "var(--accent-strong)",
      }}
    >
      {label}
    </div>
  );
  const totalRow = (label, value) => (
    <div
      className="crow"
      style={{
        background: "var(--primary-soft)",
        borderRadius: "var(--r-md)",
        padding: "10px 14px",
        marginTop: 8,
      }}
    >
      <strong style={{ color: "var(--accent-strong)" }}>{label}</strong>
      <strong style={{ color: "var(--accent-strong)" }}>{som(value)}</strong>
    </div>
  );

  // Строка блока = вид расхода. Клик открывает его траты за период: вносить и
  // править их можно прямо здесь, не уходя со страницы отчёта.
  const kindRow = (row) => (
    <button
      key={row.id}
      className="ghost kind-row"
      onClick={() => setOpenKind(row)}
      title={t("kinds.openHint")}
    >
      <span className="k">
        {row.name}
        {!row.in_profit && (
          <span className="muted" style={{ fontSize: 12 }}> · {t("finance.notInProfit")}</span>
        )}
        {/* Часть суммы система посчитала сама — говорим, откуда она взялась,
            чтобы цифра не выглядела появившейся ниоткуда. */}
        {Number(row.auto_amount) > 0 && (
          <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
            {t("finance.autoFromStock", { value: som(row.auto_amount) })}
            {Number(row.manual_amount) > 0 &&
              ` + ${t("finance.autoManualPart", { value: som(row.manual_amount) })}`}
          </div>
        )}
      </span>
      <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <span style={row.in_profit ? undefined : { color: "var(--ink-muted)" }}>{som(row.amount)}</span>
        <Icon name="chevron-right" size={14} />
      </span>
    </button>
  );

  // Строка блока «Материалы» по коду встроенного вида — чтобы держать порядок
  // строк как в Excel. Своих видов это не касается, они идут ниже общим списком.
  const MATERIAL_BUILTINS = ["MATERIAL_PURCHASE", "TRANSPORT", "MATERIAL_DEBT"];
  const materialRows = report.materials?.rows || [];
  const materialRow = (code) => {
    const row = materialRows.find((r) => r.code === code);
    return row ? kindRow(row) : null;
  };
  const otherMaterialRows = materialRows.filter((r) => !MATERIAL_BUILTINS.includes(r.code));

  const addKindButton = (block) => (
    <button
      className="ghost"
      style={{ marginTop: 6, color: "var(--accent-strong)", fontWeight: 600 }}
      onClick={() => setEditKind({ block })}
    >
      + {t("kinds.add")}
    </button>
  );

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 12 }}>
        <h1 style={{ margin: 0 }}>{t("nav.finance")}</h1>
        <div className="row" style={{ gap: 10, alignItems: "flex-end", margin: 0 }}>
          <MonthPicker value={period} onChange={setPeriod} />
          <button
            className={period.month ? "secondary" : ""}
            onClick={() =>
              setPeriod(
                period.month
                  ? { year: period.year, month: null }
                  : { year: now.getFullYear(), month: now.getMonth() + 1 }
              )
            }
          >
            {period.month ? t("finance.allTime") : t("finance.thisMonth")}
          </button>
        </div>
      </div>
      <p className="muted" style={{ marginTop: 4 }}>
        {period.month ? t("finance.periodHint") : t("finance.allTimeActive")}
      </p>

      <div className="stat-grid" style={{ marginBottom: 18 }}>
        {/* Выручка — по всем заказам периода, включая отданные в долг. Под
            цифрой сразу видно, сколько из неё уже на руках: без этого «выручка
            300 000» при 200 000 долга читается как 300 000 деньгами. */}
        <Stat
          label={t("finance.revenue")}
          value={som(report.revenue)}
          sub={`${t("finance.revenuePaid")}: ${som(report.revenue_paid)} · ${t("finance.revenueDebt")}: ${som(report.client_debt)}`}
        />
        <Stat label={t("finance.expenses")} value={som(report.total_expenses)} />
        <Stat
          label={t("finance.profit")}
          value={som(report.profit)}
          color={Number(report.profit) >= 0 ? "ok" : "danger"}
        />
        <Stat label={t("finance.clientDebt")} value={som(report.client_debt)} color="accent-strong" />
      </div>

      <DailyProfitChart year={period.year} month={period.month} reloadKey={revision} />

      {/* Три блока с подытогами — структура как в Excel заказчика. Порядок
          строк «Материалов» тоже его: начало · закуп · конец · транспорт ·
          долг, поэтому известные виды расставлены поимённо, а свои добавленные
          идут следом. */}
      {report.materials && (
        <div className="card" style={{ marginTop: 16 }}>
          {blockHead(t("finance.blockMaterials"))}
          {/* Остатков на начало и на конец в блоке больше нет (просьба
              заказчика): расход материала — это то, что за месяц потратили,
              закуп плюс транспорт. */}
          {materialRow("MATERIAL_PURCHASE")}
          {materialRow("TRANSPORT")}
          {materialRow("MATERIAL_DEBT")}
          {otherMaterialRows.map(kindRow)}
          {addKindButton("MATERIALS")}
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
            {t("finance.materialsHint")}
          </p>
          {totalRow(t("finance.totalMaterials"), report.materials.total)}
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        {blockHead(t("finance.blockFixed"))}
        {(report.fixed.rows || []).map(kindRow)}
        {addKindButton("FIXED")}
        {totalRow(t("finance.totalFixed"), report.fixed.total)}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        {blockHead(t("finance.blockVariable"))}
        {(report.variable.rows || []).map(kindRow)}
        {addKindButton("VARIABLE")}
        {totalRow(t("finance.totalVariable"), report.variable.total)}
        {report.investments && Number(report.investments.total) > 0 && (
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
            {t("finance.investmentsHint")} — {som(report.investments.total)}
          </p>
        )}
      </div>

      {/* Себестоимость проданного и маржа: сколько осталось от выручки после
          закупочной стоимости материала, ещё до аренды и прочих расходов. */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("finance.cogsTitle")}</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.cogsHint")}</p>
        <div className="crow"><span className="k">{t("finance.revenue")}</span><span>{som(report.revenue)}</span></div>
        <div className="crow">
          <span className="k">{t("finance.cogs")}</span>
          <span style={{ color: "var(--danger)" }}>− {som(report.cogs)}</span>
        </div>
        {totalRow(t("finance.grossMargin"), report.gross_margin)}
      </div>

      {/* Карточки «Реквизиты для документов» здесь больше нет: заказчик просил
          убрать реквизиты совсем, и печатные формы их больше не печатают.
          Данные в базе остались — если понадобятся, блок возвращается назад. */}

      {/* Закрытие периода: после него отчёт за месяц перестаёт меняться. */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("period.title")}</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("period.hint")}</p>
        {lock.closed_through ? (
          <p style={{ margin: "0 0 12px", color: "var(--accent-strong)", fontWeight: 600 }}>
            <Icon name="lock" size={15} />{" "}
            {t("period.closedThrough", { date: new Date(lock.closed_through).toLocaleDateString("ru-RU") })}
            {lock.updated_by_name ? <span className="muted" style={{ fontWeight: 400 }}> · {lock.updated_by_name}</span> : null}
          </p>
        ) : (
          <p className="muted" style={{ margin: "0 0 12px" }}>{t("period.open")}</p>
        )}
        {/* Замок ставит и снимает только админ — бухгалтеру показываем
            состояние периода, но не форму: сервер его PATCH всё равно
            отклоняет, а кнопка обещала обратное. */}
        {!readOnly && (
          <div className="row" style={{ gap: 10, alignItems: "flex-end", margin: 0, flexWrap: "wrap" }}>
            <div className="field" style={{ margin: 0, width: 190 }}>
              <label>{t("period.closeThrough")}</label>
              <input
                type="date"
                value={lockDraft}
                max={new Date().toLocaleDateString("sv-SE")}
                onChange={(e) => setLockDraft(e.target.value)}
              />
            </div>
            <button onClick={() => saveLock(lockDraft)} disabled={!lockDraft}>
              {t("period.close")}
            </button>
            {lock.closed_through && (
              <button className="secondary" onClick={() => saveLock(null)}>
                {t("period.open_")}
              </button>
            )}
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("finance.referralTitle")}</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.referralHint")}</p>
        {editRow(t("finance.referralBonus"), "referral_bonus")}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("finance.cuttingTitle")}</h3>
        {/* Блок читался непонятно: три цифры подряд без единого слова о том,
            что они значат. Объяснение стоит прямо здесь, а не в документации —
            искать его никто не пойдёт. */}
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("finance.cuttingHint")}</p>
        <div className="stat-grid">
          {/* ГЛАВНАЯ величина — квадратные метры, а не сумма: вопрос «сколько
              наработал станок» это про объём работы, а деньги с него —
              следствие ставки. Сумма и погонные метры ушли под цифру. */}
          <Stat
            label={t("finance.cuttingTotal")}
            value={`${q2(report.cutting?.area)} ${t("finance.sqmShort")}`}
            sub={t("finance.cuttingVolumeArea", {
              pm: q2(report.cutting?.running_meters),
              sum: som(report.cutting?.total),
            })}
          />
          {/* Разбивка по СТАНКАМ: показываем только те, на которых в периоде
              реально резали, — пустые карточки ничего не сообщают. */}
          {(report.cutting?.rows || []).map((row) => (
            <Stat
              key={row.id ?? "none"}
              label={row.name}
              value={`${q2(row.area)} ${t("finance.sqmShort")}`}
              sub={t("finance.cuttingVolumeArea", {
                pm: q2(row.running_meters),
                sum: som(row.amount),
              })}
            />
          ))}
        </div>

        {/* Кто сколько отрезал. Считается по тому, кто ОФОРМИЛ заказ — поля
            «мастер за станком» в системе нет, и выдавать одно за другое
            нельзя, поэтому так и подписано. */}
        {(report.cutting?.by_user || []).length > 0 && (
          <>
            <h4 style={{ margin: "18px 0 2px" }}>{t("finance.cuttingByUser")}</h4>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              {t("finance.cuttingByUserHint")}
            </p>
            <div className="stat-grid">
              {report.cutting.by_user.map((u) => (
                <Stat
                  key={u.id ?? "none"}
                  label={u.name}
                  value={`${q2(u.area)} ${t("finance.sqmShort")}`}
                  sub={t("finance.cuttingUserPm", { pm: q2(u.running_meters) })}
                />
              ))}
            </div>
          </>
        )}
      </div>

      {/* Складской лист заказчика («остаток в начале месяца · поступление ·
          остаток в конце · проданные · производство») живёт на Складе
          отдельной вкладкой во весь экран — здесь на него только ссылка. */}
      <div className="card" style={{ marginTop: 16 }}>
        <h3>{t("stockSheet.title")}</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("stockSheet.hint")}</p>
        <Link to="/admin/catalog?tab=sheet" className="btn-link">
          {t("stockSheet.linkFromFinance")} <Icon name="arrow-right" size={16} />
        </Link>
      </div>

      <details className="card" style={{ marginTop: 16 }}>
        <summary style={{ cursor: "pointer", fontWeight: 600, color: "var(--accent-strong)" }}>
          {t("finance.materialReportTitle")}
        </summary>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-end", marginTop: 12, gap: 10, flexWrap: "wrap" }}>
          <div className="field" style={{ margin: 0 }}>
            <label>{t("clients.filterDay")}</label>
            <input type="date" value={matDay} onChange={(e) => setMatDay(e.target.value)} />
          </div>
          <div className="field" style={{ margin: 0, minWidth: 220 }}>
            <label>{t("finance.filterMaterial")}</label>
            <select value={matFilter} onChange={(e) => setMatFilter(e.target.value)}>
              <option value="">{t("common.all")}</option>
              {matReport.map((r) => (
                <option key={r.id} value={r.id}>{r.name}</option>
              ))}
            </select>
          </div>
          <button className="secondary" onClick={downloadCsv} disabled={!filteredMat.length}>
            {t("finance.downloadCsv")}
          </button>
        </div>
        <div style={{ marginTop: 12, overflowX: "auto" }}>
          <DataTable columns={matColumns} rows={filteredMat} />
          {/* ИТОГО по всем материалам. При фильтре по одному материалу не
              показываем — там итог совпал бы с единственной строкой. */}
          {matTotals && !matFilter && (
            <div
              className="crow"
              style={{
                background: "var(--primary-soft)",
                borderRadius: "var(--r-md)",
                padding: "10px 14px",
                marginTop: 8,
                flexWrap: "wrap",
                gap: 12,
              }}
            >
              <strong style={{ color: "var(--accent-strong)" }}>{t("finance.totalRow")}</strong>
              <span>
                <span className="muted">{t("finance.colOrders")}:</span> <strong>{matTotals.orders}</strong>
                {" · "}
                <span className="muted">{t("finance.colSoldArea")}:</span> <strong>{q2(matTotals.sold_area)}</strong>
                {" · "}
                <span className="muted">{t("finance.colMatSum")}:</span> <strong>{som(matTotals.material_revenue)}</strong>
                {" · "}
                <span className="muted">{t("finance.colCutSum")}:</span>{" "}
                <strong style={{ color: "var(--accent-strong)" }}>{som(matTotals.cut_revenue)}</strong>
                {" · "}
                <span className="muted">{t("finance.colReceived")}:</span> <strong>{q2(matTotals.received)}</strong>
              </span>
            </div>
          )}
        </div>
      </details>

      {/* ОДИН список всех трат за период. Раньше здесь стояли три секции —
          постоянные, зарплаты, покупки, — и у каждой свой ряд плиток с суммами
          по видам. Те же виды с теми же суммами уже перечислены в блоках отчёта
          выше, поэтому страница заканчивалась тремя экранами нулей подряд.
          Диалог по клику на строку отчёта показывает один вид, а этот список —
          весь месяц сразу, поэтому он и остался, но уже один. */}
      <div className="card" style={{ marginTop: 16 }}>
        {/* reloadKey — тот же счётчик правок, что у графика по дням: трату
            заводят в блоке отчёта выше, и без него список внизу продолжал
            показывать старое, пока страницу не перезагрузят. */}
        <ExpenseListSection
          title={t("finance.allExpensesTitle")}
          subtitle={t("finance.allExpensesHint")}
          kinds={[...fixedKinds, ...salaryKinds, ...purchaseKinds]}
          period={params}
          reloadKey={revision}
          onChanged={reloadAll}
        />
      </div>

      {openKind && (
        <ExpenseKindModal
          kind={openKind}
          period={params}
          onClose={() => setOpenKind(null)}
          onChanged={reloadAll}
          onEditKind={(k) => {
            setOpenKind(null);
            setEditKind({ kind: k });
          }}
        />
      )}
      {editKind && (
        <ExpenseKindFormModal
          kind={editKind.kind}
          block={editKind.block}
          onClose={() => setEditKind(null)}
          onSaved={reloadAll}
        />
      )}
    </>
  );
}
