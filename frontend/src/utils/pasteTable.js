// Вставка блока из Excel (Ctrl+V): строки разделены переводом строки, ячейки —
// табом. Числа в русском Excel пишут с запятой и пробелом в тысячах
// («2 679», «1,22», «2 679,50»), и обычное Number() роняет их в NaN — ячейка
// выглядит заполненной, а на деле пуста.

/** Число из ячейки Excel; пусто или не число — null. */
export function parseNumber(raw) {
  if (raw == null) return null;
  let s = String(raw).replace(/[\s  ]/g, "");
  if (!s) return null;
  const comma = s.lastIndexOf(",");
  const dot = s.lastIndexOf(".");
  if (comma >= 0 && dot >= 0) {
    // «1.234,56» (запятая последняя) — точка это разряды; «1,234.56» — наоборот.
    s = comma > dot ? s.replace(/\./g, "").replace(",", ".") : s.replace(/,/g, "");
  } else if (comma >= 0) {
    s = s.replace(",", ".");
  }
  if (!/^[+-]?\d*\.?\d+$/.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** Текст из буфера → массив строк-массивов. Пустые строки отбрасываются. */
export function parseTable(text) {
  return String(text || "")
    .replace(/\r/g, "")
    .split("\n")
    .map((line) => line.split("\t").map((c) => c.trim()))
    .filter((cells) => cells.some((c) => c !== ""));
}

/** Похоже ли на блок таблицы (а не на одно слово, вставленное в поле). */
export const looksLikeTable = (text) => /[\t\n]/.test(String(text || "").replace(/\n$/, ""));

/** Число для отправки на сервер: без хвоста вида 1200.0000000002. */
export const money2 = (n) => Math.round(n * 100) / 100;
