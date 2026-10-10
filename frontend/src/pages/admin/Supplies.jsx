import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../../api/api.js";
import { apiError } from "../../api/errors.js";
import { useAuth } from "../../auth/AuthContext.jsx";
import DataTable from "../../components/DataTable.jsx";
import Icon from "../../components/Icon.jsx";
import Modal from "../../components/Modal.jsx";
import PrintSupply from "../../components/PrintSupply.jsx";
import RefSelect from "../../components/RefSelect.jsx";
import { useUI } from "../../components/UIProvider.jsx";
import WasteModal from "../../components/WasteModal.jsx";
import LotCorrectionModal from "../../components/LotCorrectionModal.jsx";
import PaySupplierModal from "../../components/PaySupplierModal.jsx";
import SupplyReturnModal from "../../components/SupplyReturnModal.jsx";
import SuppliersPanel from "../../components/SuppliersPanel.jsx";
import { formatDate, formatMoney, formatNumber } from "../../utils/format.js";
import { looksLikeTable, money2, parseNumber, parseTable } from "../../utils/pasteTable.js";
import Tabs from "../../components/Tabs.jsx";
import Field, { focusFirstInvalid } from "../../components/Field.jsx";

// Приходные накладные — поставка целиком, одним документом.
//
// Раньше приход вводился по одной позиции с кнопки на строке материала:
// поставка на восемь позиций — восемь отдельных операций, и сверить итог с
// бумажной накладной было нечем. Здесь строки вводятся сеткой, а сумма по
// бумаге стоит рядом с суммой системы: сошлось или нет, видно сразу.

const som = (n) => formatMoney(n);
const q2 = (n) => formatNumber(n, { max: 2 });
const today = () => new Date().toLocaleDateString("sv-SE");

const EMPTY_LINE = {
  material: "", form: "SHEET",
  width: "", height: "", length: "", sheet_count: "", quantity: "", cost: "", code: "",
};

/** Сколько единиц встанет на склад по строке — та же формула, что на сервере. */
function lineQuantity(line, material) {
  if (!material) return 0;
  const n = (v) => Number(v) || 0;
  if (!material.is_roll_material || line.form === "QTY") return n(line.quantity);
  if (line.form === "ROLL") return n(line.width) * n(line.length);
  return n(line.width) * n(line.height) * n(line.sheet_count);
}

export default function Supplies({ embedded = false }) {
  const { t } = useTranslation();
  const { toast, confirm } = useUI();
  const { isAdmin, seesMoney } = useAuth();
  const [rows, setRows] = useState([]);
  // Второй раздел экрана — ОТХОД (брак): те же мерки, что у прихода, но в
  // минус. Стоит рядом с приёмкой, потому что брак видит тот, кто принимает
  // товар и стоит у станка, — складовщик.
  const [section, setSection] = useState("intake");
  const [waste, setWaste] = useState([]);
  const [wasteOpen, setWasteOpen] = useState(false);
  const [suppliers, setSuppliers] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [open, setOpen] = useState(null);   // просмотр накладной
  // Строка накладной, в которой правят опечатку («Исправить приход»).
  const [fixing, setFixing] = useState(null);
  // Перенос даты накладной. Механика на сервере была с 18.08 (партии и журнал
  // едут за датой), но добраться до неё можно было только запросом в API:
  // в окне документа дата стояла текстом. Поставка, внесённая не тем днём, —
  // случай частый, а «отмените и заведите заново» стоит дороже.
  const [movingDate, setMovingDate] = useState("");
  const [movingBusy, setMovingBusy] = useState(false);
  const [draft, setDraft] = useState(null); // новая накладная
  const [printing, setPrinting] = useState(null); // печатная форма накладной
  const [busy, setBusy] = useState(false);
  // Ошибки строк накладной подсвечиваются после первой попытки сохранить.
  const [showProblems, setShowProblems] = useState(false);
  // Оплата поставщику по накладной и возврат товара поставщику (карточка).
  const [paying, setPaying] = useState(null);
  const [returning, setReturning] = useState(null);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState("");

  function load() {
    api.get("/warehouse/supplies/", { params: { page_size: 100 } })
      .then((r) => setRows(r.data.results || r.data))
      .catch(() => toast(t("common.error"), "error"));
  }
  function loadSuppliers() {
    return api.get("/warehouse/suppliers/").then((r) => setSuppliers(r.data.results || r.data));
  }
  // Отходы — записи журнала «Списание»: отход и есть списание, только со
  // своей причиной и мерками ввода. Отдельного документа у него нет.
  function loadWaste() {
    api.get("/warehouse/inventory-logs/", { params: { type: "WRITE_OFF", page_size: 100 } })
      .then((r) => setWaste(r.data.results || r.data))
      .catch(() => {});
  }
  useEffect(() => {
    load();
    loadSuppliers();
    loadWaste();
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
      .then((r) => setMaterials(r.data.results));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const matById = useMemo(
    () => Object.fromEntries(materials.map((m) => [String(m.id), m])),
    [materials]
  );

  // --- новая накладная ----------------------------------------------------
  function startDraft() {
    setDraft({
      number: "", supplier: "", received_on: today(), stated_total: "",
      // Счёт по умолчанию — наличные: сервер не принимает оплату без счёта
      // (она не попадала в кассу), а «забыл выбрать» хуже, чем лишний щелчок.
      paid_amount: "", paid_account: "CASH", note: "",
      currency: "KGS", rate: "", is_opening: false,
      lines: [{ ...EMPTY_LINE }],
    });
  }
  const foreign = !!draft && draft.currency !== "KGS";
  const setField = (k) => (v) => setDraft((d) => ({ ...d, [k]: v }));
  function setLine(i, patch) {
    setDraft((d) => ({
      ...d,
      lines: d.lines.map((l, idx) => (idx === i ? { ...l, ...patch } : l)),
    }));
  }
  function addLine() {
    setDraft((d) => ({ ...d, lines: [...d.lines, { ...EMPTY_LINE }] }));
  }
  function dropLine(i) {
    setDraft((d) => ({ ...d, lines: d.lines.filter((_, idx) => idx !== i) }));
  }
  // Материал сам подсказывает форму прихода: акрил листами, плёнка рулоном,
  // крепёж количеством. Складовщик её меняет только когда привезли иначе.
  // Ширина партии — из карточки: у листа размер листа, у рулона ширина рулона.
  // Раньше рулону ширину не подставляли, и складовщик набирал её руками при
  // каждой приёмке (1.5 при карточке 1.2 — и обрезок, площадь резки, надпись
  // в кассе считались по разным ширинам).
  const presetDims = (m, form) => ({
    width:
      form === "SHEET" && m?.sheet_width ? String(m.sheet_width)
      : form === "ROLL" && m?.roll_width ? String(m.roll_width)
      : "",
    height: form === "SHEET" && m?.sheet_height ? String(m.sheet_height) : "",
  });
  function pickMaterial(i, id) {
    const m = matById[String(id)];
    const form = !m ? "SHEET" : !m.is_roll_material ? "QTY" : m.intake_form || "SHEET";
    setLine(i, { material: id, form, ...presetDims(m, form) });
  }
  function pickForm(i, form) {
    const m = matById[String(draft.lines[i]?.material)];
    setLine(i, { form, ...presetDims(m, form) });
  }
  // Ширина, отличная от карточки, — законно (под одной карточкой лежат рулоны
  // разной ширины), но должна бросаться в глаза: чаще это опечатка.
  const widthDiffers = (l, m) =>
    l.form === "ROLL" && m?.roll_width && l.width !== "" && Number(l.width) !== Number(m.roll_width);

  // Строка «начата» — в ней что-то вписано; «полная» — есть материал и сумма
  // (сумма 0 допустима явно — подарок поставщика, но не пустое поле). Раньше
  // строка без суммы МОЛЧА выпадала из накладной: восемь позиций на бумаге,
  // у одной забыли сумму — «Оприходовать (7)», и позиция не приходила на
  // склад. Теперь недозаполненная строка держит кнопку закрытой и называет
  // себя.
  const started = (l) =>
    l.material || l.cost !== "" || l.width !== "" || l.height !== "" || l.length !== "" ||
    l.sheet_count !== "" || l.quantity !== "" || l.code !== "";
  const lineProblem = (l) => {
    if (!started(l)) return null;
    if (!l.material) return t("supplies.lineNeedsMaterial");
    if (l.cost === "" || Number.isNaN(Number(l.cost)) || Number(l.cost) < 0) return t("supplies.lineNeedsCost");
    return null;
  };
  const problems = (draft?.lines || [])
    .map((l, i) => ({ i, text: lineProblem(l) }))
    .filter((x) => x.text);
  const filled = (draft?.lines || []).filter((l) => started(l) && !lineProblem(l));
  const draftTotal = filled.reduce((s, l) => s + Number(l.cost || 0), 0);
  const stated = draft?.stated_total === "" ? null : Number(draft?.stated_total);
  const diff = stated == null ? 0 : stated - draftTotal;

  const ratePositive = Number(draft?.rate) > 0;

  async function save(force = false) {
    // Ошибки строк уже видны под полями; здесь — фокус на первое неверное.
    if (problems.length) {
      setShowProblems(true);
      return focusFirstInvalid();
    }
    if (!filled.length) {
      setShowProblems(true);
      toast(t("supplies.needLines"), "error");
      return;
    }
    if (foreign && !ratePositive) {
      toast(t("supplies.rateNeeded", { cur: draft.currency }), "error");
      return;
    }
    setBusy(true);
    try {
      const paid = draft.is_opening ? 0 : Number(draft.paid_amount) || 0;
      const payload = {
        number: draft.number,
        supplier: draft.supplier || null,
        received_on: draft.received_on,
        stated_total: draft.stated_total === "" ? null : Number(draft.stated_total),
        paid_amount: paid,
        // Счёт нужен только когда что-то заплатили; без суммы он ничего не значит.
        paid_account: paid > 0 ? draft.paid_account : "",
        note: draft.note,
        is_opening: !!draft.is_opening,
        currency: draft.currency,
        ...(foreign ? { rate: Number(draft.rate) } : {}),
        ...(force === true ? { force: true } : {}),
        lines: filled.map((l) => ({
          material: Number(l.material),
          form: l.form,
          width: l.width === "" ? null : Number(l.width),
          height: l.height === "" ? null : Number(l.height),
          length: l.length === "" ? null : Number(l.length),
          sheet_count: l.sheet_count === "" ? null : Number(l.sheet_count),
          quantity: l.quantity === "" ? 0 : Number(l.quantity),
          // В валюте накладной сумму строки считает сервер: сом = валюта × курс.
          ...(foreign ? { cost_fc: Number(l.cost) } : { cost: Number(l.cost) }),
          code: l.code,
        })),
      };
      const { data: posted } = await api.post("/warehouse/supplies/", payload);
      // Лист не того размера, что в карточке (F7), — сказать сразу, пока
      // пачка ещё у ворот: продаётся лист по площади из карточки.
      (posted?.warnings || []).forEach((w) => toast(w.message, "error"));
      setDraft(null);
      load();
      toast(t("supplies.posted"));
    } catch (e) {
      const dup = e.response?.status === 409 && e.response?.data?.code === "duplicate_supply";
      if (dup) {
        // Похоже на двойной ввод той же бумажной накладной (20 листов вместо 10).
        // Решает человек: другая поставка — проводим с подтверждением.
        setBusy(false);
        if (await confirm(e.response.data.detail)) {
          return save(true);
        }
        return;
      }
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  // --- вставка блока из Excel: материал · количество · цена за единицу --------
  const findMaterial = (name) => {
    const q = String(name || "").trim().toLowerCase();
    if (!q) return null;
    const exact = materials.find((m) => m.name.trim().toLowerCase() === q);
    if (exact) return exact;
    const part = materials.filter((m) => m.name.toLowerCase().includes(q));
    return part.length === 1 ? part[0] : null;
  };
  function pasteLines(text) {
    const table = parseTable(text);
    const made = [];
    const unknown = [];
    let bad = 0;
    for (const cells of table) {
      const qty = parseNumber(cells[1]);
      const price = parseNumber(cells[2]);
      // Шапка таблицы («Материал · Кол-во · Цена») числом не читается — пропускаем.
      if (qty == null && price == null) {
        if (made.length || table.indexOf(cells) > 0) bad += 1;
        continue;
      }
      const m = findMaterial(cells[0]);
      if (!m) unknown.push(cells[0] || "—");
      const form = !m ? "SHEET" : !m.is_roll_material ? "QTY" : m.intake_form || "SHEET";
      const line = { ...EMPTY_LINE, material: m ? m.id : "", form, ...presetDims(m, form) };
      if (!m || !m.is_roll_material || form === "QTY") line.quantity = qty == null ? "" : String(qty);
      else if (form === "ROLL") line.length = qty == null ? "" : String(qty);
      else line.sheet_count = qty == null ? "" : String(qty);
      if (qty != null && price != null) line.cost = String(money2(qty * price));
      made.push(line);
    }
    if (!made.length) {
      toast(t("supplies.pasteNothing"), "error");
      return false;
    }
    setDraft((d) => {
      const empty = d.lines.every((l) => !started(l));
      return { ...d, lines: empty ? made : [...d.lines, ...made] };
    });
    toast(t("supplies.pasted", { n: made.length }));
    if (unknown.length) toast(t("supplies.pasteUnknown", { names: unknown.slice(0, 5).join(", ") }), "error");
    if (bad) toast(t("supplies.pasteSkipped", { n: bad }), "error");
    return true;
  }
  function onGridPaste(e) {
    const text = e.clipboardData?.getData("text/plain") || "";
    // Одно слово или число в одно поле вставляется как обычно; блок таблицы —
    // строками накладной.
    if (!looksLikeTable(text)) return;
    e.preventDefault();
    pasteLines(text);
  }

  async function moveDate() {
    if (!open || !movingDate || movingDate === open.received_on) return;
    setMovingBusy(true);
    try {
      const { data } = await api.patch(`/warehouse/supplies/${open.id}/`, { received_on: movingDate });
      setOpen(data);
      setMovingDate("");
      toast(t("supplies.dateMoved"));
      load();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setMovingBusy(false);
    }
  }

  async function cancelSupply(row) {
    if (!(await confirm(t("supplies.cancelConfirm", { n: row.number || `#${row.id}` })))) return;
    try {
      await api.delete(`/warehouse/supplies/${row.id}/`);
      setOpen(null);
      load();
      toast(t("supplies.cancelled"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  async function deletePayment(p) {
    if (!(await confirm(t("supplies.deletePaymentConfirm", { sum: som(p.amount), date: formatDate(p.paid_on) })))) return;
    try {
      await api.delete(`/warehouse/supplier-payments/${p.id}/`);
      const { data } = await api.get(`/warehouse/supplies/${open.id}/`);
      setOpen(data);
      load();
      toast(t("supplies.paymentDeleted"));
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    }
  }

  const columns = [
    {
      key: "number",
      label: t("supplies.number"),
      render: (r) => (
        <>
          <strong>{r.number || `#${r.id}`}</strong>
          {r.is_opening ? <span className="badge" style={{ marginLeft: 6 }}>{t("supplies.openingBadge")}</span> : null}
          {r.currency && r.currency !== "KGS" ? <span className="badge" style={{ marginLeft: 6 }}>{r.currency}</span> : null}
          {r.possible_duplicate_of ? (
            <span className="badge warn" style={{ marginLeft: 6 }} title={t("supplies.dupeHint", { n: r.possible_duplicate_of })}>
              {t("supplies.dupeBadge")}
            </span>
          ) : null}
          {r.note ? <div className="muted" style={{ fontSize: 12 }}>{r.note}</div> : null}
        </>
      ),
    },
    { key: "received_on", label: t("supplies.date"), render: (r) => formatDate(r.received_on) },
    { key: "supplier_name", label: t("supplies.supplier"), render: (r) => r.supplier_name || <span className="muted">—</span> },
    { key: "lines", label: t("supplies.positions"), render: (r) => r.lines.length },
    // Закупочные цены, суммы и долг — администратору; складовщик принимает товар
    // (сервер всё равно отдаёт ему пустые поля).
    ...(isAdmin
      ? [
          { key: "total_cost", label: t("supplies.total"), render: (r) => som(r.total_cost) },
          {
            key: "discrepancy",
            label: t("supplies.diff"),
            // Ради этой колонки документ и заведён: сошлось с бумагой или нет.
            render: (r) =>
              r.stated_total == null ? (
                <span className="muted">—</span>
              ) : Number(r.discrepancy) === 0 ? (
                <span className="badge ok">{t("supplies.matches")}</span>
              ) : (
                <span style={{ color: "var(--danger-ink)", fontWeight: 600 }}>
                  {Number(r.discrepancy) > 0 ? "+" : ""}{formatNumber(r.discrepancy, { max: 2 })}
                </span>
              ),
          },
          {
            key: "debt",
            label: t("supplies.debt"),
            render: (r) =>
              r.is_opening ? (
                <span className="muted">—</span>
              ) : Number(r.debt) > 0 ? (
                <span style={{ color: "var(--danger-ink)", fontWeight: 600 }}>
                  {som(r.debt)}
                  {r.debt_foreign != null && Number(r.debt_foreign) > 0 ? (
                    <div className="muted" style={{ fontSize: 12, fontWeight: 400 }}>
                      {formatNumber(r.debt_foreign, { max: 2 })} {r.currency}
                    </div>
                  ) : null}
                </span>
              ) : Number(r.overpaid) > 0 ? (
                <span className="badge ok" title={t("supplies.creditHint")}>{t("supplies.credit", { sum: som(r.overpaid) })}</span>
              ) : (
                <span className="badge ok">{t("supplies.paid")}</span>
              ),
          },
        ]
      : []),
    {
      key: "actions",
      label: "",
      render: (r) => (
        <button className="secondary row-btn" onClick={() => setOpen(r)}>
          {t("supplies.openDoc")}
        </button>
      ),
    },
  ];

  const totalDebt = rows.reduce((s, r) => s + Number(r.debt || 0), 0);
  // Карточку поставщика (сальдо, платежи) видит тот, кому открыты деньги.
  const sections = [
    { key: "intake", label: t("waste.tabIntake") },
    { key: "waste", label: t("waste.tabWaste") },
    ...(seesMoney ? [{ key: "suppliers", label: t("suppliers.tab") }] : []),
  ];

  // --- отходы ----------------------------------------------------------------
  // Сколько ушло со склада — метрами у рулона (так операцию мерили), иначе
  // в единице материала: кв.м у листа, своя у штучного.
  const wasteAmount = (r) => {
    if (r.metres_changed != null) return `${q2(-Number(r.metres_changed))} ${t("unit.METER")}`;
    const unit = r.material_is_roll ? t("unit.SQM") : t(`unit.${r.material_unit}`);
    return `${q2(-Number(r.quantity_changed))} ${unit}`;
  };
  const fmtDay = (iso) => formatDate(iso);
  const monthKey = today().slice(0, 7);
  const wasteMonth = waste.filter((r) => new Date(r.happened_at).toLocaleDateString("sv-SE").slice(0, 7) === monthKey);
  const wasteMonthCost = wasteMonth.reduce((s, r) => s + Number(r.cost || 0), 0);
  const wasteColumns = [
    { key: "happened_at", label: t("waste.date"), render: (r) => fmtDay(r.happened_at) },
    { key: "material_name", label: t("checkout.material"), render: (r) => <strong>{r.material_name}</strong> },
    {
      key: "quantity_changed",
      label: t("waste.amount"),
      render: (r) => <span style={{ color: "var(--danger-ink)", fontWeight: 600, whiteSpace: "nowrap" }}>−{wasteAmount(r)}</span>,
    },
    // Себестоимость — только тем, кто видит деньги: складовщик записывает
    // брак, но почём цех его купил, ему знать незачем.
    ...(seesMoney
      ? [{ key: "cost", label: t("waste.cost"), render: (r) => (r.cost == null ? <span className="muted">—</span> : som(r.cost)) }]
      : []),
    { key: "reason", label: t("waste.reason"), render: (r) => <span className="muted journal-reason" title={r.reason || ""}>{r.reason || "—"}</span> },
    { key: "created_by_username", label: t("waste.who"), render: (r) => r.created_by_username || "—" },
  ];

  return (
    <>
      {/* В складском разделе страница стоит сама по себе (не вкладкой «Склада»)
          и получает свой заголовок. Приход вводит тот, кто принимает товар, —
          складовщик; раньше у него этого экрана не было вовсе. */}
      {!embedded && <h1>{t("nav.supply")}</h1>}
      {/* Приход и отход — два раздела одного экрана: мерки одни, знак разный. */}
      <Tabs
        id="supplies"
        panel={false}
        label={t("nav.supply")}
        style={{ marginTop: 0 }}
        value={section}
        onChange={setSection}
        tabs={sections}
      />

      {section === "intake" && (
        <>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <p className="muted" style={{ fontSize: 13, margin: 0, maxWidth: "60ch" }}>
              {t("supplies.hint")}
            </p>
            <button onClick={startDraft}>+ {t("supplies.newDoc")}</button>
          </div>

          <div className="stat-grid" style={{ margin: "14px 0" }}>
            <div className="stat">
              <div className="label">{t("supplies.statDocs")}</div>
              <div className="value">{rows.length}</div>
            </div>
            {isAdmin && (
              <>
                <div className="stat">
                  <div className="label">{t("supplies.statSum")}</div>
                  <div className="value">
                    {som(rows.filter((r) => !r.is_opening).reduce((s, r) => s + Number(r.total_cost || 0), 0))}
                  </div>
                </div>
                <div className="stat">
                  <div className="label">{t("supplies.statDebt")}</div>
                  <div className="value" style={totalDebt > 0 ? { color: "var(--danger-ink)" } : undefined}>
                    {som(totalDebt)}
                  </div>
                </div>
              </>
            )}
          </div>

          <DataTable
            columns={columns}
            rows={rows}
            rowClass={(r) => (r.possible_duplicate_of ? "warn" : "")}
          />
        </>
      )}

      {section === "waste" && (
        <>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <p className="muted" style={{ fontSize: 13, margin: 0, maxWidth: "60ch" }}>
              {t("waste.hint")}
            </p>
            <button onClick={() => setWasteOpen(true)}>+ {t("waste.new")}</button>
          </div>

          <div className="stat-grid" style={{ margin: "14px 0" }}>
            <div className="stat">
              <div className="label">{t("waste.statMonth")}</div>
              <div className="value">{wasteMonth.length} <span className="muted" style={{ fontSize: 13 }}>{t("waste.entries")}</span></div>
            </div>
            {seesMoney && (
              <div className="stat">
                <div className="label">{t("waste.statMonthCost")}</div>
                <div className="value" style={wasteMonthCost > 0 ? { color: "var(--danger-ink)" } : undefined}>
                  {som(wasteMonthCost)}
                </div>
              </div>
            )}
          </div>

          <h3 style={{ margin: "0 0 8px" }}>{t("waste.list")}</h3>
          <DataTable columns={wasteColumns} rows={waste} empty={t("waste.empty")} />
        </>
      )}

      {section === "suppliers" && seesMoney && (
        <SuppliersPanel
          canWrite={isAdmin}
          onOpenSupply={(id) => {
            api.get(`/warehouse/supplies/${id}/`).then((r) => setOpen(r.data)).catch(() => {});
          }}
          onChanged={load}
        />
      )}

      {wasteOpen && (
        <WasteModal
          materials={materials}
          onClose={() => setWasteOpen(false)}
          onDone={() => {
            loadWaste();
            // Остатки в выпадашках материалов — свежие, иначе второй отход
            // подряд проверялся бы по старому остатку.
            api
              .get("/warehouse/materials/", { params: { ordering: "name", page_size: 500 } })
              .then((r) => setMaterials(r.data.results));
          }}
        />
      )}

      {/* --- Просмотр накладной --- */}
      {open && (
        <Modal
          wide
          title={`${t("supplies.docTitle")} ${open.number || `#${open.id}`}`}
          onClose={() => setOpen(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setPrinting(open)}>
                <Icon name="printer" size={16} /> {t("print.print")}
              </button>
              {isAdmin && !open.is_opening && Number(open.debt) > 0 && (
                <button onClick={() => setPaying(open)}>{t("supplies.payBtn")}</button>
              )}
              {isAdmin && !open.is_opening && (
                <button className="secondary" onClick={() => setReturning(open)}>{t("supplies.returnBtn")}</button>
              )}
              {isAdmin && (
                <button className="ghost row-danger" onClick={() => cancelSupply(open)}>
                  <Icon name="trash" size={16} /> {t("supplies.cancel")}
                </button>
              )}
            </>
          }
        >
          <div className="crow">
            <span className="k">{t("supplies.date")}</span>
            {isAdmin ? (
              <span className="row" style={{ gap: 6, margin: 0, alignItems: "center" }}>
                <input
                  type="date"
                  value={movingDate || open.received_on}
                  onChange={(e) => setMovingDate(e.target.value)}
                  style={{ width: 150 }}
                />
                {movingDate && movingDate !== open.received_on && (
                  <button className="secondary row-btn" onClick={moveDate} disabled={movingBusy}>
                    {t("supplies.moveDate")}
                  </button>
                )}
              </span>
            ) : (
              <span>{formatDate(open.received_on)}</span>
            )}
          </div>
          <div className="crow"><span className="k">{t("supplies.supplier")}</span><span>{open.supplier_name || "—"}</span></div>
          {open.is_opening && (
            <p className="callout" style={{ margin: "8px 0" }}>{t("supplies.openingNote")}</p>
          )}
          {isAdmin && (
            <>
              <div className="crow"><span className="k">{t("supplies.total")}</span><strong>{som(open.total_cost)}</strong></div>
              {open.currency !== "KGS" && (
                <div className="crow">
                  <span className="k">{t("supplies.inCurrency", { cur: open.currency })}</span>
                  <span>{formatNumber(open.total_foreign, { max: 2 })} {open.currency} · {t("supplies.atRate", { rate: formatNumber(open.rate, { max: 4 }) })}</span>
                </div>
              )}
              {open.stated_total != null && (
                <div className="crow">
                  <span className="k">{t("supplies.statedTotal")}</span>
                  <span>
                    {formatNumber(open.stated_total, { max: 2 })} {open.currency !== "KGS" ? open.currency : "сом"}{" "}
                    {Number(open.discrepancy) === 0 ? (
                      <span className="badge ok">{t("supplies.matches")}</span>
                    ) : (
                      <span style={{ color: "var(--danger-ink)" }}>
                        ({t("supplies.diff")} {Number(open.discrepancy) > 0 ? "+" : ""}{formatNumber(open.discrepancy, { max: 2 })})
                      </span>
                    )}
                  </span>
                </div>
              )}
              {!open.is_opening && (
                <>
                  <div className="crow"><span className="k">{t("supplies.paidTo")}</span><span>{som(open.paid_total)}</span></div>
                  <div className="crow">
                    <span className="k">{t("supplies.debt")}</span>
                    <strong style={Number(open.debt) > 0 ? { color: "var(--danger-ink)" } : undefined}>
                      {som(open.debt)}
                      {open.debt_foreign != null && Number(open.debt) > 0 ? ` · ${formatNumber(open.debt_foreign, { max: 2 })} ${open.currency}` : ""}
                    </strong>
                  </div>
                  {Number(open.overpaid) > 0 && (
                    <div className="crow"><span className="k">{t("supplies.creditLabel")}</span><strong style={{ color: "var(--ok-ink)" }}>{som(open.overpaid)}</strong></div>
                  )}
                </>
              )}
            </>
          )}

          {/* Пять колонок на телефон не влезают — прокручиваем таблицу, а не
              выталкиваем за экран саму модалку. */}
          <div className="table-scroll" style={{ marginTop: 14 }}>
          <table className="table plain-table">
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("supplies.received")}</th>
                {isAdmin && <th>{t("supplies.lineCost")}</th>}
                {isAdmin && <th>{t("supplies.unitCost")}</th>}
                <th>{t("supply.rollCode")}</th>
                {isAdmin && <th />}
              </tr>
            </thead>
            <tbody>
              {open.lines.map((l) => (
                <tr key={l.id}>
                  <td><strong>{l.material_name}</strong></td>
                  <td>{q2(l.quantity)} {l.unit}</td>
                  {isAdmin && (
                    <td>
                      {som(l.cost)}
                      {l.cost_fc != null && open.currency !== "KGS" ? (
                        <div className="muted" style={{ fontSize: 12 }}>{formatNumber(l.cost_fc, { max: 2 })} {open.currency}</div>
                      ) : null}
                    </td>
                  )}
                  {isAdmin && <td>{q2(l.unit_cost)} <span className="muted">{"сом"}/{l.unit}</span></td>}
                  <td className="muted">{l.code || "—"}</td>
                  {isAdmin && (
                    <td>
                      <button className="secondary row-btn" onClick={() => setFixing(l)}>
                        {t("lotFix.button")}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          {isAdmin && !open.is_opening && (
            <>
              <h3 style={{ margin: "16px 0 6px" }}>{t("supplies.paymentsTitle")}</h3>
              {Number(open.paid_amount) > 0 && (
                <div className="crow">
                  <span className="k">{t("supplies.legacyPaid")}</span>
                  <span>{som(open.paid_amount)}{open.paid_account ? ` · ${t(`suppliersDebt.${open.paid_account === "CASH" ? "cash" : "bank"}`)}` : ""}</span>
                </div>
              )}
              {(open.payments || []).map((p) => (
                <div key={p.id} className="crow">
                  <span className="k">
                    {formatDate(p.paid_on)} · {p.kind_label}
                    {p.note ? ` · ${p.note}` : ""}
                  </span>
                  <span>
                    {som(p.amount)}
                    {p.account ? ` · ${t(`suppliersDebt.${p.account === "CASH" ? "cash" : "bank"}`)}` : ""}
                    {p.amount_fc != null ? ` · ${formatNumber(p.amount_fc, { max: 2 })} ${p.currency} ${t("supplies.atRate", { rate: formatNumber(p.rate, { max: 4 }) })}` : ""}
                    {Number(p.fx_diff) !== 0 ? ` · ${t("supplies.fxDiff")} ${Number(p.fx_diff) > 0 ? "+" : ""}${formatNumber(p.fx_diff, { max: 2 })}` : ""}
                    {" "}
                    <button className="ghost row-danger" aria-label={t("common.delete")} onClick={() => deletePayment(p)}>
                      <Icon name="trash" size={14} />
                    </button>
                  </span>
                </div>
              ))}
              {!Number(open.paid_amount) && !(open.payments || []).length && (
                <p className="muted" style={{ fontSize: 13, margin: "4px 0" }}>{t("supplies.noPayments")}</p>
              )}
              {(open.returns || []).length > 0 && (
                <>
                  <h3 style={{ margin: "16px 0 6px" }}>{t("supplies.returnsTitle")}</h3>
                  {open.returns.map((r) => (
                    <div key={r.id} className="crow">
                      <span className="k">{formatDate(r.returned_on)} · {r.lines.map((l) => l.label).join("; ")}</span>
                      <span>
                        −{som(r.amount)}
                        {Number(r.refund) > 0 ? ` · ${t("supplies.refunded", { sum: som(r.refund) })}` : ` · ${t("supplies.kept")}`}
                      </span>
                    </div>
                  ))}
                </>
              )}
            </>
          )}
          <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
            {t("supplies.editHint")}
          </p>
        </Modal>
      )}

      {paying && (
        <PaySupplierModal
          row={{
            kind: "SUPPLY", id: paying.id, label: `${t("supplies.docTitle")} ${paying.number || `#${paying.id}`}`,
            supplier: paying.supplier_name, debt: paying.debt, currency: paying.currency,
            debt_foreign: paying.debt_foreign, supply_rate: paying.rate,
          }}
          onClose={() => setPaying(null)}
          onPaid={() => {
            const id = paying.id;
            setPaying(null);
            api.get(`/warehouse/supplies/${id}/`).then((r) => setOpen(r.data)).catch(() => {});
            load();
          }}
        />
      )}

      {returning && (
        <SupplyReturnModal
          supply={returning}
          onClose={() => setReturning(null)}
          onDone={(data) => {
            setReturning(null);
            setOpen(data);
            load();
          }}
        />
      )}

      {fixing && open && (
        <LotCorrectionModal
          lot={{
            ...(fixing.roll ? { roll: fixing.roll } : { supply_line: fixing.id }),
            form: fixing.roll ? fixing.form : "QTY",
            width: fixing.width,
            height: fixing.height,
            length: fixing.length,
            sheet_count: fixing.sheet_count,
            quantity: fixing.quantity,
            cost: fixing.cost,
            unit: t(`unit.${fixing.unit_code}`),
            title: `${t("supplies.docTitle")} ${open.number || `#${open.id}`} · ${fixing.material_name}`,
          }}
          onClose={() => setFixing(null)}
          onDone={() => {
            api.get(`/warehouse/supplies/${open.id}/`).then((r) => setOpen(r.data)).catch(() => {});
            load();
          }}
        />
      )}

      {printing && <PrintSupply supply={printing} onClose={() => setPrinting(null)} />}

      {/* --- Новая накладная --- */}
      {draft && (
        <Modal
          wide
          title={t("supplies.newDoc")}
          onClose={() => setDraft(null)}
          footer={
            <>
              <button className="secondary" onClick={() => setDraft(null)}>{t("common.cancel")}</button>
              <button onClick={() => save()} disabled={busy || !filled.length || problems.length > 0}>
                {busy ? t("common.loading") : t("supplies.post", { n: filled.length })}
              </button>
            </>
          }
        >
          <div className="row">
            <Field className="grow" style={{ margin: 0 }} label={t("supplies.number")}>
              <input
                value={draft.number}
                onChange={(e) => setField("number")(e.target.value)}
                placeholder={t("supplies.numberPh")}
              />
            </Field>
            <div className="field grow" style={{ margin: 0 }}>
              <label>{t("supplies.supplier")}</label>
              <RefSelect
                value={draft.supplier}
                options={suppliers}
                endpoint="/warehouse/suppliers/"
                onCreated={loadSuppliers}
                onChange={(v) => setField("supplier")(v ? Number(v) : "")}
              />
            </div>
            <Field style={{ margin: 0, width: 170 }} label={t("supplies.date")}>
              <input type="date" value={draft.received_on} onChange={(e) => setField("received_on")(e.target.value)} />
            </Field>
          </div>

          {isAdmin && (
            <div className="row" style={{ marginTop: 10, gap: 14, alignItems: "flex-end", flexWrap: "wrap" }}>
              <Field style={{ margin: 0, width: 120 }} label={t("supplies.currency")}>
                <select
                  value={draft.currency}
                  disabled={draft.is_opening}
                  onChange={(e) => setDraft((d) => ({ ...d, currency: e.target.value, rate: e.target.value === "KGS" ? "" : d.rate }))}
                >
                  {["KGS", "USD", "EUR", "RUB", "CNY", "KZT"].map((c) => (
                    <option key={c} value={c}>{c === "KGS" ? t("supplies.currencySom") : c}</option>
                  ))}
                </select>
              </Field>
              {foreign && (
                <Field
                  style={{ margin: 0, width: 190 }}
                  label={t("supplies.rate", { cur: draft.currency })}
                  error={showProblems && !ratePositive ? t("supplies.rateNeeded", { cur: draft.currency }) : undefined}
                >
                  <input
                    type="number" step="any" inputMode="decimal" value={draft.rate}
                    onChange={(e) => setField("rate")(e.target.value)}
                  />
                </Field>
              )}
              <label className="row" style={{ margin: 0, gap: 6, alignItems: "center", cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={draft.is_opening}
                  onChange={(e) => setDraft((d) => ({
                    ...d, is_opening: e.target.checked,
                    ...(e.target.checked ? { currency: "KGS", rate: "", paid_amount: "" } : {}),
                  }))}
                />
                <span>{t("supplies.openingMode")}</span>
              </label>
            </div>
          )}
          {draft.is_opening && (
            <p className="callout" style={{ margin: "10px 0 0" }}>{t("supplies.openingModeHint")}</p>
          )}
          {foreign && (
            <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
              {t("supplies.foreignHint", { cur: draft.currency })}
            </p>
          )}

          <div className="row" style={{ justifyContent: "space-between", alignItems: "center", margin: "14px 0 0" }}>
            <span className="muted" style={{ fontSize: 12 }}>{t("supplies.pasteHint")}</span>
            <button type="button" className="secondary row-btn" onClick={() => setPasteOpen(true)}>
              {t("supplies.pasteBtn")}
            </button>
          </div>

          {/* Сетка позиций: столько строк, сколько в бумажной накладной. Блок из
              Excel (Ctrl+V) в любое поле сетки раскладывается по строкам. */}
          <div className="grid-wrap" style={{ marginTop: 8 }} onPaste={onGridPaste}>
            <table className="table grid-table">
              <thead>
                <tr>
                  <th style={{ minWidth: 220 }}>{t("checkout.material")}</th>
                  <th style={{ width: 120 }}>{t("supply.form")}</th>
                  <th style={{ width: 240 }}>{t("supplies.size")}</th>
                  <th style={{ width: 110 }}>{t("supplies.received")}</th>
                  <th style={{ width: 120 }}>{foreign ? `${t("supplies.lineCost")}, ${draft.currency}` : t("supplies.lineCost")}</th>
                  <th style={{ width: 130 }}>{t("supply.rollCode")}</th>
                  <th style={{ width: 40 }} />
                </tr>
              </thead>
              <tbody>
                {draft.lines.map((l, i) => {
                  const m = matById[String(l.material)];
                  const qty = lineQuantity(l, m);
                  const unit = !m ? "" : !m.is_roll_material || l.form === "QTY" ? t(`unit.${m.unit}`) : t("unit.SQM");
                  return (
                    <tr key={i}>
                      <td>
                        <select
                          aria-label={`${t("checkout.material")} ${i + 1}`}
                          aria-invalid={showProblems && started(l) && !l.material ? true : undefined}
                          value={l.material}
                          onChange={(e) => pickMaterial(i, e.target.value)}
                        >
                          <option value="">—</option>
                          {materials.map((x) => (
                            <option key={x.id} value={x.id}>{x.name}</option>
                          ))}
                        </select>
                      </td>
                      <td>
                        <select
                          aria-label={`${t("supply.form")} ${i + 1}`}
                          value={l.form}
                          disabled={!m || !m.is_roll_material}
                          onChange={(e) => pickForm(i, e.target.value)}
                        >
                          <option value="SHEET">{t("supply.formSheet")}</option>
                          <option value="ROLL">{t("supply.formRoll")}</option>
                          <option value="QTY">{t("supply.formQty")}</option>
                        </select>
                      </td>
                      <td>
                        {!m || !m.is_roll_material || l.form === "QTY" ? (
                          <input
                            type="number" step="any" value={l.quantity}
                            placeholder={t("supplies.qtyPh")}
                            onChange={(e) => setLine(i, { quantity: e.target.value })}
                          />
                        ) : l.form === "ROLL" ? (
                          <>
                            <div className="size-cell">
                              <input type="number" step="any" value={l.width} placeholder={t("supply.width")} onChange={(e) => setLine(i, { width: e.target.value })} />
                              <input type="number" step="any" value={l.length} placeholder={t("supply.length")} onChange={(e) => setLine(i, { length: e.target.value })} />
                            </div>
                            {widthDiffers(l, m) && (
                              <div style={{ color: "var(--danger-ink)", fontSize: 11, marginTop: 2 }}>
                                {t("supplies.widthDiffers", { width: m.roll_width })}
                              </div>
                            )}
                          </>
                        ) : (
                          <div className="size-cell three">
                            <input type="number" step="any" value={l.width} placeholder={t("supply.width")} onChange={(e) => setLine(i, { width: e.target.value })} />
                            <input type="number" step="any" value={l.height} placeholder={t("supply.height")} onChange={(e) => setLine(i, { height: e.target.value })} />
                            <input type="number" step="any" value={l.sheet_count} placeholder={t("supply.sheets")} onChange={(e) => setLine(i, { sheet_count: e.target.value })} />
                          </div>
                        )}
                      </td>
                      <td className="muted" style={{ whiteSpace: "nowrap" }}>
                        {qty > 0 ? `${q2(qty)} ${unit}` : "—"}
                      </td>
                      <td>
                        <input
                          type="number"
                          step="any"
                          inputMode="decimal"
                          aria-label={`${t("supplies.lineCost")} ${i + 1}`}
                          aria-invalid={lineProblem(l) && l.material ? true : undefined}
                          aria-describedby={lineProblem(l) ? `line-err-${i}` : undefined}
                          value={l.cost}
                          onChange={(e) => setLine(i, { cost: e.target.value })}
                          style={lineProblem(l) && l.material ? { borderColor: "var(--danger-ink)" } : undefined}
                        />
                        {lineProblem(l) && (
                          <div id={`line-err-${i}`} style={{ color: "var(--danger-ink)", fontSize: 12, marginTop: 2, whiteSpace: "nowrap" }}>{lineProblem(l)}</div>
                        )}
                      </td>
                      <td>
                        <input aria-label={`${t("supply.rollCode")} ${i + 1}`} value={l.code} onChange={(e) => setLine(i, { code: e.target.value })} />
                      </td>
                      <td>
                        {draft.lines.length > 1 && (
                          <button className="ghost" onClick={() => dropLine(i)} aria-label={t("common.delete")}>
                            <Icon name="x" size={16} />
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <button className="ghost" style={{ marginTop: 8, color: "var(--accent-ink)", fontWeight: 600 }} onClick={addLine}>
            + {t("supplies.addLine")}
          </button>
          {problems.length > 0 && (
            <p role="alert" style={{ color: "var(--danger-ink)", fontSize: 13, margin: "8px 0 0" }}>
              {t("supplies.linesIncomplete", { rows: problems.map((x) => x.i + 1).join(", ") })}
            </p>
          )}

          {/* Сверка с бумагой — ради неё документ и существует. */}
          <div className="card" style={{ background: "var(--canvas)", padding: 12, marginTop: 14 }}>
            <div className="crow">
              <span className="k">{t("supplies.total")}</span>
              <strong>
                {foreign ? `${formatNumber(draftTotal, { max: 2 })} ${draft.currency}` : som(draftTotal)}
                {foreign && ratePositive ? ` ≈ ${som(draftTotal * Number(draft.rate))}` : ""}
              </strong>
            </div>
            <div className="row" style={{ margin: "8px 0 0", gap: 10, flexWrap: "wrap" }}>
              <Field style={{ margin: 0, width: 190 }} label={foreign ? `${t("supplies.statedTotal")}, ${draft.currency}` : t("supplies.statedTotal")}>
                <input
                  type="number" step="any" value={draft.stated_total}
                  placeholder={t("supplies.statedPh")}
                  onChange={(e) => setField("stated_total")(e.target.value)}
                />
              </Field>
              {isAdmin && !draft.is_opening && (
                <>
                  <Field style={{ margin: 0, width: 190 }} label={foreign ? `${t("supplies.paidTo")}, ${t("supplies.currencySom")}` : t("supplies.paidTo")}>
                    <input
                      type="number" step="any" value={draft.paid_amount}
                      placeholder="0"
                      onChange={(e) => setField("paid_amount")(e.target.value)}
                    />
                  </Field>
                  {Number(draft.paid_amount) > 0 && (
                    <Field style={{ margin: 0, width: 150 }} label={t("suppliersDebt.account")}>
                      <select value={draft.paid_account} onChange={(e) => setField("paid_account")(e.target.value)}>
                        <option value="CASH">{t("suppliersDebt.cash")}</option>
                        <option value="BANK">{t("suppliersDebt.bank")}</option>
                      </select>
                    </Field>
                  )}
                </>
              )}
              <Field className="grow" style={{ margin: 0 }} label={t("supplies.note")}>
                <input value={draft.note} onChange={(e) => setField("note")(e.target.value)} />
              </Field>
            </div>
            {stated != null && stated > 0 && (
              <p style={{ margin: "10px 0 0", fontSize: 14, color: diff === 0 ? "var(--ok-ink)" : "var(--danger-ink)" }}>
                {diff === 0 ? t("supplies.matchesFull") : t("supplies.diffFull", { sum: som(Math.abs(diff)) })}
              </p>
            )}
          </div>
        </Modal>
      )}

      {pasteOpen && (
        <Modal
          title={t("supplies.pasteBtn")}
          onClose={() => { setPasteOpen(false); setPasteText(""); }}
          footer={
            <>
              <button className="secondary" onClick={() => { setPasteOpen(false); setPasteText(""); }}>{t("common.cancel")}</button>
              <button
                disabled={!pasteText.trim()}
                onClick={() => {
                  if (pasteLines(pasteText)) { setPasteOpen(false); setPasteText(""); }
                }}
              >
                {t("supplies.pasteApply")}
              </button>
            </>
          }
        >
          <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>{t("supplies.pasteHelp")}</p>
          <textarea
            autoFocus rows={8} style={{ width: "100%" }} value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            aria-label={t("supplies.pasteBtn")}
          />
        </Modal>
      )}
    </>
  );
}
