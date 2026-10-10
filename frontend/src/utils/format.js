// Единое форматирование чисел, денег и дат.
//
// Раньше в каждом экране жила своя копия `const som = ...toLocaleString("ru-RU")`
// (Чеки, Касса, Финансы, Клиенты, Касса-продажа…), а где-то и вовсе `toFixed(0)`
// или «3000.00»: один и тот же итог выглядел то как «1 879 сом», то как
// «1879», то как «1,879». Теперь формат один и следует языку интерфейса.
//
// Что согласовано:
//   * ru и ky — пробел (неразрывный) между разрядами, запятая у дроби, дата
//     ДД.ММ.ГГГГ. Для кыргызского не берём системный шаблон ky-KG: он выдаёт
//     «2026-07-10» (год-день-месяц), то есть 7 октября читается как 10 июля.
//   * en — запятая между разрядами, точка у дроби, дата ДД/ММ/ГГГГ (en-GB, а
//     не en-US: у нас день стоит перед месяцем во всех трёх языках).
//   * между числом и «сом» — неразрывный пробел, поэтому «сом» не уезжает на
//     следующую строку. Слово «сом» не переводится.
import i18n from "../i18n";

const NBSP = " ";

const NUM_LOCALE = { ru: "ru-RU", ky: "ky-KG", en: "en-GB" };
const DATE_LOCALE = { ru: "ru-RU", ky: "ru-RU", en: "en-GB" };

const lang = () => {
  const l = String(i18n.resolvedLanguage || i18n.language || "ru").slice(0, 2);
  return NUM_LOCALE[l] ? l : "ru";
};

const numberFormats = new Map();
function numberFormat(l, min, max) {
  const key = `${l}|${min}|${max}`;
  let f = numberFormats.get(key);
  if (!f) {
    f = new Intl.NumberFormat(NUM_LOCALE[l], { minimumFractionDigits: min, maximumFractionDigits: max });
    numberFormats.set(key, f);
  }
  return f;
}

const toNumber = (n) => {
  const v = typeof n === "number" ? n : Number(n);
  return Number.isFinite(v) ? v : 0;
};

/** Число с разрядами по языку интерфейса. По умолчанию — целое, округлённое. */
export function formatNumber(n, { min = 0, max = min } = {}) {
  const v = toNumber(n);
  // Минус нуля («-0») в интерфейсе не нужен.
  const out = numberFormat(lang(), min, Math.max(min, max)).format(v);
  return /^[-−]0([.,]0+)?$/.test(out) ? out.replace(/^[-−]/, "") : out;
}

/** Деньги: «1 879 сом». `fraction: 2` — с копейками («3 000,00 сом»). */
export function formatMoney(n, { fraction = 0 } = {}) {
  return `${formatNumber(n, { min: fraction, max: fraction })}${NBSP}сом`;
}

// «2026-10-07» без времени парсится как UTC и в часовых поясах западнее
// Гринвича показывал бы вчерашний день — собираем из частей, локально.
function toDate(d) {
  if (d instanceof Date) return d;
  if (typeof d === "string" && /^\d{4}-\d{2}-\d{2}$/.test(d)) {
    const [y, m, day] = d.split("-").map(Number);
    return new Date(y, m - 1, day, 12);
  }
  return new Date(d);
}

const pad = (n) => String(n).padStart(2, "0");

/** Дата: ДД.ММ.ГГГГ (en: ДД/ММ/ГГГГ). Пустое или битое значение — пустая строка. */
export function formatDate(d) {
  if (d === null || d === undefined || d === "") return "";
  const dt = toDate(d);
  if (Number.isNaN(dt.getTime())) return "";
  const sep = lang() === "en" ? "/" : ".";
  return `${pad(dt.getDate())}${sep}${pad(dt.getMonth() + 1)}${sep}${dt.getFullYear()}`;
}

/** Время ЧЧ:ММ, 24 часа. */
export function formatTime(d) {
  if (d === null || d === undefined || d === "") return "";
  const dt = toDate(d);
  if (Number.isNaN(dt.getTime())) return "";
  return `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
}

export function formatDateTime(d) {
  const date = formatDate(d);
  return date ? `${date} ${formatTime(d)}` : "";
}

/** Системная локаль для редких случаев, где нужен Intl напрямую (названия месяцев). */
export const dateLocale = () => DATE_LOCALE[lang()];
