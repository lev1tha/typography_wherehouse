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
import { useUI } from "./UIProvider.jsx";

const trim = (v) => String(v).replace(/\.?0+$/, "").replace(".", ",");

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

export default function CatalogGrid({ types, sites, onDone, onClose }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [rows, setRows] = useState(() => Array.from({ length: 8 }, () => ({ ...BLANK })));
  const [errors, setErrors] = useState({});   // {rowIndex: {field: [сообщение]}}
  const [busy, setBusy] = useState(false);
  const gridRef = useRef(null);

  // Колонки сгруппированы шапкой в два яруса — как в складском листе заказчика:
  // одиннадцать равнозначных заголовков подряд читаются как стена, а «Материал ·
  // Лист · Цены» видно с одного взгляда. Подписи внутри группы короткие: группа
  // уже сказала, о чём речь, и колонки от этого влезают на экран без прокрутки.
  const COLS = useMemo(() => [
    { key: "name", label: t("grid.name"), width: 168, sticky: true },
    { key: "type", label: t("warehouse.type"), width: 104, options: types, group: "material" },
    { key: "color", label: t("warehouse.color"), width: 96, group: "material" },
    // Единицу видно из группы («Лист, м», «Цены, сом»), а у толщины своей группы
    // нет — миллиметры остаются подсказкой на заголовке.
    { key: "thickness_mm", label: t("grid.thickness"), hint: t("warehouse.thickness"), width: 68, num: true, group: "material" },
    { key: "article", label: t("warehouse.article"), width: 68, group: "material" },
    { key: "production", label: t("grid.production"), width: 104, options: sites, group: "material" },
    { key: "sheet_width", label: t("grid.width"), width: 70, num: true, group: "sheet" },
    { key: "sheet_height", label: t("grid.height"), width: 70, num: true, group: "sheet" },
    { key: "piece_price", label: t("grid.piecePrice"), width: 86, num: true, group: "price" },
    { key: "price_per_sqm", label: t("grid.sqmPrice"), width: 86, num: true, group: "price" },
    { key: "cut_rate_per_pm", label: t("grid.cutRate"), width: 86, num: true, group: "price" },
  ], [types, sites, t]);

  // Шапка первого яруса: подряд идущие колонки одной группы под общим заголовком.
  const GROUPS = useMemo(() => {
    const titles = {
      material: t("grid.groupMaterial"),
      sheet: t("grid.groupSheet"),
      price: t("grid.groupPrice"),
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
          next[target][col.key] = col.options
            ? matchOption(col.options, value)
            : value.trim();
        });
      });
      if (!isEmptyRow(next[next.length - 1])) next.push({ ...BLANK });
      return next;
    });
    setErrors({});
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

  async function save() {
    if (!filled.length) return;
    setBusy(true);
    setErrors({});
    try {
      const payload = filled.map((row) => {
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
        return out;
      });
      const r = await api.post("/warehouse/materials/bulk/", { rows: payload });
      toast(t("grid.saved", { count: r.data.created }));
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
                      <select
                        data-cell={`${rowIndex}-${colIndex}`}
                        value={row[col.key]}
                        onChange={(e) => setCell(rowIndex, col.key, e.target.value)}
                        onPaste={(e) => handlePaste(e, rowIndex, colIndex)}
                      >
                        <option value="">—</option>
                        {col.options.map((option) => (
                          <option key={option.id} value={option.id}>{option.name}</option>
                        ))}
                        {/* Вставили название, которого нет в справочнике —
                            показываем как есть, иначе ячейка выглядит пустой. */}
                        {row[col.key] &&
                          !col.options.some((o) => String(o.id) === String(row[col.key])) && (
                            <option value={row[col.key]}>{row[col.key]} — ?</option>
                          )}
                      </select>
                    ) : (
                      <input
                        data-cell={`${rowIndex}-${colIndex}`}
                        type={col.num ? "number" : "text"}
                        step={col.num ? "any" : undefined}
                        value={row[col.key]}
                        placeholder={col.key === "name" ? suggestedName(row, types) : ""}
                        // Собранное название длиннее ячейки — показываем целиком
                        // по наведению, обрезанное «Форекс молочный 8 м…» не
                        // даёт понять, тот ли это материал.
                        title={col.key === "name" ? row.name || suggestedName(row, types) : undefined}
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

      <div className="row" style={{ marginTop: 16, justifyContent: "space-between", alignItems: "center" }}>
        <span className="muted">{t("grid.readyCount", { count: filled.length })}</span>
        <div className="row" style={{ margin: 0, gap: 10 }}>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={save} disabled={busy || !filled.length}>
            {busy ? t("common.loading") : t("grid.save", { count: filled.length })}
          </button>
        </div>
      </div>
    </>
  );
}
