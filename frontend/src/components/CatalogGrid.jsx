/**
 * Массовый ввод каталога — таблица «строка = материал».
 *
 * Заказчик пришёл из Excel: его номенклатура это полсотни строк, и заводить их
 * модалкой по одной он не станет. Здесь всё как в таблице: Tab между полями,
 * Enter вниз, вставка куска таблицы из буфера (Ctrl+V) сразу в несколько
 * строк и столбцов. Сохраняется одним запросом — всё или ничего, чтобы
 * опечатка в 47-й строке не оставила в базе 46 материалов.
 *
 * Чего в сетке НЕТ намеренно: закупочной цены (приходит с поступлением партии),
 * галки «листовой» (выводится из размера листа) и площади листа (считается
 * из размера). Всё это система знает сама.
 */
import { useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { formatNumber } from "../utils/format.js";
import { parseNumber } from "../utils/pasteTable.js";
import RefSelect from "./RefSelect.jsx";
import { useUI } from "./UIProvider.jsx";

const trim = (v) => String(v).replace(/\.?0+$/, "").replace(".", ",");

/** Число из ячейки русского Excel (XL-01): «1,22», «2 679», «2 679,50 сом».
 * Пусто → "", не число → null, иначе строка с точкой для сервера. Ячейки —
 * текстовые (inputMode="decimal"), а не type=number: тот на «1,22» отдаёт
 * пустую строку, и ячейка выглядела пустой, хотя в ней лежал текст. */
function numText(raw) {
  const s = String(raw ?? "").trim().replace(/\s*(сомов|сома|сом|som)\.?$/i, "");
  if (!s) return "";
  const n = parseNumber(s);
  return n === null ? null : String(n);
}

// Название, которое соберётся само, если ячейку оставить пустой.
// Не экспортируется намеренно: лишний экспорт из файла с компонентом ломает
// горячую перезагрузку Vite («Could not Fast Refresh»).
function suggestedName(row, types) {
  const type = types.find((x) => String(x.id) === String(row.type) || x.name === row.type);
  const parts = [type?.name || "", row.color || ""];
  if (row.thickness_mm) parts.push(`${trim(row.thickness_mm)} мм`);
  if (row.article) parts.push(row.article);
  if (row.sheet_width && row.sheet_height) {
    parts.push(`${trim(row.sheet_width)}×${trim(row.sheet_height)}`);
  }
  return parts.filter(Boolean).join(" ").trim();
}

const BLANK = {
  name: "", type: "", color: "", thickness_mm: "", article: "",
  sheet_width: "", sheet_height: "", production: "", piece_price: "",
  price_per_sqm: "", cut_rate_per_pm: "",
  // Рулон: ширина полотна и цена за погонный метр. Отдельной колонки «форма»
  // нет намеренно — заполненная ширина рулона И ЕСТЬ признак рулона, ровно
  // так же, как размер листа означает лист.
  roll_width: "", price_per_pm: "",
};

const isEmptyRow = (row) => Object.values(row).every((v) => String(v ?? "").trim() === "");

/** Вставленный текст справочника («Форекс») → ключ выбранного пункта.
 *
 * Без этого ячейка после вставки показывала «—»: в списке лежат ключи, а из
 * буфера приходит название. Сервер название понимает, но человек видел пустоту
 * и думал, что вставка не сработала. Не нашли — оставляем текст как есть, он
 * будет виден в ячейке, а сервер объяснит, что такого типа нет. */
function matchOption(options, text) {
  const raw = String(text).trim();
  const hit = options.find((o) => o.name.trim().toLowerCase() === raw.toLowerCase());
  return hit ? String(hit.id) : raw;
}

export default function CatalogGrid({ types, sites, onDone, onClose, onRefsChanged }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [rows, setRows] = useState(() => Array.from({ length: 8 }, () => ({ ...BLANK })));
  const [errors, setErrors] = useState({});   // {rowIndex: {field: [сообщение]}}
  const [busy, setBusy] = useState(false);
  // «Обновить существующие по названию» (XL-05): та же вставка из Excel меняет
  // цены у материалов, которые уже есть, — сначала предпросмотр «было → стало».
  const [upsert, setUpsert] = useState(false);
  const [preview, setPreview] = useState(null);
  const gridRef = useRef(null);

  // Колонки сгруппированы шапкой в два яруса — как в складском листе заказчика:
  // одиннадцать равнозначных заголовков подряд читаются как стена, а «Материал ·
  // Лист · Цены» видно с одного взгляда. Подписи внутри группы короткие: группа
  // уже сказала, о чём речь, и колонки от этого влезают на экран без прокрутки.
  const COLS = useMemo(() => [
    { key: "name", label: t("grid.name"), width: 168, sticky: true },
    { key: "type", label: t("warehouse.type"), width: 104, options: types,
      endpoint: "/warehouse/material-types/", group: "material" },
    { key: "color", label: t("warehouse.color"), width: 96, group: "material" },
    // Единицу видно из группы («Лист, м», «Цены, сом»), а у толщины своей группы
    // нет — миллиметры остаются подсказкой на заголовке.
    { key: "thickness_mm", label: t("grid.thickness"), hint: t("warehouse.thickness"), width: 68, num: true, group: "material" },
    { key: "article", label: t("warehouse.article"), width: 68, group: "material" },
    { key: "production", label: t("grid.production"), width: 104, options: sites,
      endpoint: "/warehouse/production-sites/", group: "material" },
    { key: "sheet_width", label: t("grid.width"), width: 70, num: true, group: "sheet" },
    { key: "sheet_height", label: t("grid.height"), width: 70, num: true, group: "sheet" },
    { key: "piece_price", label: t("grid.piecePrice"), width: 86, num: true, group: "price" },
    { key: "price_per_sqm", label: t("grid.sqmPrice"), width: 86, num: true, group: "price" },
    { key: "cut_rate_per_pm", label: t("grid.cutRate"), width: 86, num: true, group: "price" },
    // Рулонные колонки идут ПОСЛЕДНИМИ: порядок колонок здесь — это порядок
    // столбцов при вставке из Excel, и новый столбец в середине сдвинул бы все
    // цены в готовых таблицах заказчика.
    { key: "roll_width", label: t("grid.rollWidth"), width: 80, num: true, group: "roll" },
    { key: "price_per_pm", label: t("grid.rollPrice"), width: 92, num: true, group: "roll" },
  ], [types, sites, t]);

  // Шапка первого яруса: подряд идущие колонки одной группы под общим заголовком.
  const GROUPS = useMemo(() => {
    const titles = {
      material: t("grid.groupMaterial"),
      sheet: t("grid.groupSheet"),
      price: t("grid.groupPrice"),
      roll: t("grid.groupRoll"),
    };
    const out = [];
    COLS.forEach((col) => {
      const last = out[out.length - 1];
      if (last && last.group === col.group) last.span += 1;
      else out.push({ group: col.group, title: titles[col.group] || "", span: 1 });
    });
    return out;
  }, [COLS, t]);

  function setCell(rowIndex, key, value) {
    setPreview(null);   // правка после предпросмотра — предпросмотр устарел
    setRows((prev) => {
      const next = prev.map((row, i) => (i === rowIndex ? { ...row, [key]: value } : row));
      // Печатаешь в последней строке — снизу появляется ещё одна пустая, как в
      // таблице. Кнопку «добавить строку» тогда искать не нужно.
      if (rowIndex === next.length - 1 && !isEmptyRow(next[rowIndex])) {
        next.push({ ...BLANK });
      }
      return next;
    });
    setErrors((prev) => {
      if (!prev[rowIndex]) return prev;
      const next = { ...prev };
      delete next[rowIndex];
      return next;
    });
  }

  /** Вставка из буфера: кусок таблицы разъезжается по ячейкам вправо и вниз. */
  function handlePaste(event, rowIndex, colIndex) {
    const text = event.clipboardData.getData("text/plain");
    if (!text || !/[\t\n]/.test(text)) return;   // одиночная ячейка — обычная вставка
    event.preventDefault();
    const table = text.replace(/\r/g, "").replace(/\n$/, "").split("\n").map((line) => line.split("\t"));
    setRows((prev) => {
      const next = prev.map((row) => ({ ...row }));
      table.forEach((line, dr) => {
        const target = rowIndex + dr;
        while (next.length <= target) next.push({ ...BLANK });
        line.forEach((value, dc) => {
          const col = COLS[colIndex + dc];
          if (!col) return;
          if (col.options) {
            next[target][col.key] = matchOption(col.options, value);
          } else if (col.num) {
            // Число — сразу в нормальном виде; не число — как есть, чтобы
            // человек видел, что именно вставилось, а не пустую клетку.
            const n = numText(value);
            next[target][col.key] = n === null ? value.trim() : n;
          } else {
            next[target][col.key] = value.trim();
          }
        });
      });
      if (!isEmptyRow(next[next.length - 1])) next.push({ ...BLANK });
      return next;
    });
    setErrors({});
    setPreview(null);
  }

  /** Enter — вниз по тому же столбцу, как в таблице. */
  function handleKeyDown(event, rowIndex, colIndex) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const selector = `[data-cell="${rowIndex + 1}-${colIndex}"]`;
    const below = gridRef.current?.querySelector(selector);
    if (below) below.focus();
  }

  const filled = rows.filter((row) => !isEmptyRow(row));

  /** Числа сетки → вид для сервера; ячейки, где не число, — ошибкой в ячейке. */
  function normalizeRows() {
    const bad = {};
    const out = rows.map((row, rowIndex) => {
      if (isEmptyRow(row)) return row;
      const next = { ...row };
      COLS.forEach((col) => {
        if (!col.num) return;
        const n = numText(row[col.key]);
        if (n === null) {
          bad[rowIndex] = { ...(bad[rowIndex] || {}), [col.key]: [t("stock2.notNumber", { value: row[col.key] })] };
        } else {
          next[col.key] = n;
        }
      });
      return next;
    });
    return { out, bad };
  }

  async function save(apply = false) {
    if (!filled.length) return;
    const { out: normalized, bad } = normalizeRows();
    if (Object.keys(bad).length) {
      setErrors(bad);
      toast(t("grid.hasErrors", { count: Object.keys(bad).length }), "error");
      return;
    }
    setRows(normalized);
    setBusy(true);
    setErrors({});
    try {
      const payload = normalized.filter((row) => !isEmptyRow(row)).map((row) => {
        if (upsert) return upsertRow(row);
        const sheet = row.sheet_width && row.sheet_height;
        const out = {
          name: row.name || "",
          type: row.type || null,
          color: row.color || "",
          article: row.article || "",
          thickness_mm: row.thickness_mm || null,
          sheet_width: row.sheet_width || null,
          sheet_height: row.sheet_height || null,
          production: row.production || null,
          price_per_sqm: row.price_per_sqm || 0,
          cut_rate_per_pm: row.cut_rate_per_pm || 0,
        };
        // Цена за штуку у листового материала — это цена за лист, у штучного —
        // обычная розничная цена. Колонка одна: смысл у неё один и тот же.
        if (sheet) out.piece_price = row.piece_price || 0;
        else out.price_per_unit = row.piece_price || 0;
        // Рулон: ширину полотна и цену за метр шлём, только когда ширина
        // заполнена — по ней сервер и понимает, что это рулон. Пустые поля не
        // отправляем вовсе, иначе каждый лист приезжал бы с «рулонными» нулями.
        if (row.roll_width) {
          out.roll_width = row.roll_width;
          out.price_per_pm = row.price_per_pm || 0;
        }
        return out;
      });
      if (upsert && !apply) {
        const r = await api.post("/warehouse/materials/bulk/", { rows: payload, mode: "upsert", preview: true });
        setPreview(r.data);
        return;
      }
      const r = await api.post("/warehouse/materials/bulk/", { rows: payload, ...(upsert ? { mode: "upsert" } : {}) });
      toast(upsert
        ? t("stock2.upsertDone", { created: r.data.created, updated: r.data.updated })
        : t("grid.saved", { count: r.data.created }));
      onDone?.();
    } catch (e) {
      const rowErrors = e.response?.data?.errors;
      if (Array.isArray(rowErrors)) {
        // Номера строк приходят по НЕПУСТЫМ строкам — переводим их в номера
        // строк сетки, иначе подсветка сядет не туда.
        const map = {};
        const indexes = rows.map((row, i) => (isEmptyRow(row) ? null : i)).filter((i) => i !== null);
        rowErrors.forEach((item) => {
          map[indexes[item.row]] = item.fields;
        });
        setErrors(map);
        toast(t("grid.hasErrors", { count: rowErrors.length }), "error");
      } else {
        toast(apiError(e, t("common.error")), "error");
      }
    } finally {
      setBusy(false);
    }
  }

  /** Строка для режима обновления: пустые ячейки НЕ отправляем — у
   * существующего материала пустая клетка значит «не трогать», а не ноль. */
  function upsertRow(row) {
    const out = { name: row.name || "" };
    ["type", "color", "article", "production"].forEach((k) => { if (row[k]) out[k] = row[k]; });
    COLS.forEach((col) => {
      if (col.num && row[col.key] !== "" && row[col.key] != null && col.key !== "piece_price") out[col.key] = row[col.key];
    });
    if (row.piece_price !== "" && row.piece_price != null) {
      if (row.sheet_width && row.sheet_height) out.piece_price = row.piece_price;
      else out.price_per_unit = row.piece_price;
    }
    return out;
  }

  const errorList = Object.entries(errors).flatMap(([rowIndex, fields]) =>
    Object.entries(fields).map(([field, messages]) => ({
      row: Number(rowIndex) + 1,
      field,
      text: Array.isArray(messages) ? messages[0] : String(messages),
    }))
  );

  return (
    <>
      <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>{t("grid.hint")}</p>

      <div ref={gridRef} className="grid-wrap">
        <table className="table grid-table">
          <thead>
            <tr className="grid-groups">
              <th className="grid-num" />
              <th className="grid-sticky" />
              {GROUPS.filter((g) => g.group).map((g) => (
                <th key={g.group} colSpan={g.span} className={`sheet-group grid-group-${g.group}`}>
                  {g.title}
                </th>
              ))}
            </tr>
            <tr className="grid-heads">
              <th className="grid-num" />
              {COLS.map((col) => (
                <th
                  key={col.key}
                  className={col.sticky ? "grid-sticky" : ""}
                  style={{ minWidth: col.width }}
                  title={col.hint || undefined}
                >
                  {col.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className={errors[rowIndex] ? "warn" : ""}>
                <td className="grid-num">{rowIndex + 1}</td>
                {COLS.map((col, colIndex) => (
                  <td key={col.key} className={col.sticky ? "grid-sticky" : ""}>
                    {col.options ? (
                      <RefSelect
                        data-cell={`${rowIndex}-${colIndex}`}
                        value={row[col.key]}
                        options={col.options}
                        endpoint={col.endpoint}
                        onCreated={onRefsChanged}
                        onChange={(v) => setCell(rowIndex, col.key, v)}
                        onPaste={(e) => handlePaste(e, rowIndex, colIndex)}
                      />
                    ) : (
                      <input
                        data-cell={`${rowIndex}-${colIndex}`}
                        type="text"
                        inputMode={col.num ? "decimal" : undefined}
                        value={row[col.key]}
                        placeholder={col.key === "name" ? suggestedName(row, types) : ""}
                        // Ошибка — у самой ячейки (XL-01): раньше подсвечивалась
                        // строка, а в ячейке было пусто.
                        aria-invalid={errors[rowIndex]?.[col.key] ? "true" : undefined}
                        // Собранное название длиннее ячейки — показываем целиком
                        // по наведению, обрезанное «Форекс молочный 8 м…» не
                        // даёт понять, тот ли это материал.
                        title={
                          errors[rowIndex]?.[col.key]?.[0]
                          || (col.key === "name" ? row.name || suggestedName(row, types) : undefined)
                        }
                        onChange={(e) => setCell(rowIndex, col.key, e.target.value)}
                        onPaste={(e) => handlePaste(e, rowIndex, colIndex)}
                        onKeyDown={(e) => handleKeyDown(e, rowIndex, colIndex)}
                      />
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {errorList.length > 0 && (
        <div className="card" style={{ marginTop: 12, background: "var(--warn-bg)", padding: 12 }}>
          {errorList.map((item, i) => (
            <div key={i} style={{ fontSize: 13 }}>
              <strong>{t("grid.rowNo", { row: item.row })}</strong> — {item.text}
            </div>
          ))}
        </div>
      )}

      {preview && (
        <div className="card" style={{ marginTop: 12, padding: 12 }}>
          <strong>{t("stock2.upsertPreview")}</strong>
          {preview.create.length > 0 && (
            <p style={{ fontSize: 13, margin: "6px 0" }}>
              {t("stock2.willCreate", { count: preview.create.length })}: {preview.create.join(", ")}
            </p>
          )}
          {preview.update.length > 0 ? (
            <table className="table" style={{ marginTop: 6 }}>
              <tbody>
                {preview.update.flatMap((row) =>
                  row.changes.map((c, i) => (
                    <tr key={`${row.id}-${c.field}`}>
                      <td>{i === 0 ? row.name : ""}</td>
                      <td className="muted">{c.label}</td>
                      <td>{formatNumber(c.before, { max: 2 })} → <strong>{formatNumber(c.after, { max: 2 })}</strong></td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          ) : (
            <p className="muted" style={{ fontSize: 13 }}>{t("stock2.nothingToUpdate")}</p>
          )}
          {preview.unchanged > 0 && (
            <p className="muted" style={{ fontSize: 12 }}>{t("stock2.unchanged", { count: preview.unchanged })}</p>
          )}
          <div className="row" style={{ gap: 10, justifyContent: "flex-end", marginTop: 8 }}>
            <button className="secondary" onClick={() => setPreview(null)}>{t("stock2.back")}</button>
            <button onClick={() => save(true)} disabled={busy || (!preview.create.length && !preview.update.length)}>
              {t("stock2.applyUpsert")}
            </button>
          </div>
        </div>
      )}

      <div className="row" style={{ marginTop: 16, justifyContent: "space-between", alignItems: "center" }}>
        <span className="muted">{t("grid.readyCount", { count: filled.length })}</span>
        <div className="row" style={{ margin: 0, gap: 10, alignItems: "center" }}>
          <label style={{ fontSize: 13, display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={upsert}
              onChange={(e) => { setUpsert(e.target.checked); setPreview(null); }}
            />
            {t("stock2.upsertMode")}
          </label>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={() => save(false)} disabled={busy || !filled.length || !!preview}>
            {busy ? t("common.loading") : upsert ? t("stock2.previewUpsert") : t("grid.save", { count: filled.length })}
          </button>
        </div>
      </div>
    </>
  );
}
