// Сумма прописью для печатных документов: «Четыре тысячи девятьсот пятьдесят
// четыре сома 00 тыйын».
//
// В счёте и накладной эта строка обязательна: по ней бухгалтерия сверяет цифру
// и по ней же спорят, если цифру подрисовали. Без неё документ выглядит
// самодельным, а заказчик, пришедший из 1С, ждёт её на своём месте — сразу под
// таблицей позиций.

const ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"];
// Тысяча — женского рода: «одна тысяча», «две тысячи».
const ONES_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"];
const TEENS = [
  "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
  "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
];
const TENS = [
  "", "", "двадцать", "тридцать", "сорок", "пятьдесят",
  "шестьдесят", "семьдесят", "восемьдесят", "девяносто",
];
const HUNDREDS = [
  "", "сто", "двести", "триста", "четыреста", "пятьсот",
  "шестьсот", "семьсот", "восемьсот", "девятьсот",
];

// Формы существительного для 1 / 2-4 / 5-20: «сом, сома, сомов».
const SOM = ["сом", "сома", "сомов"];
const THOUSAND = ["тысяча", "тысячи", "тысяч"];
const MILLION = ["миллион", "миллиона", "миллионов"];
const BILLION = ["миллиард", "миллиарда", "миллиардов"];

/** Нужная форма существительного для числа: 21 сом, 22 сома, 25 сомов. */
export function plural(n, forms) {
  const abs = Math.abs(Math.trunc(n));
  const tail100 = abs % 100;
  if (tail100 >= 11 && tail100 <= 19) return forms[2];
  const tail10 = abs % 10;
  if (tail10 === 1) return forms[0];
  if (tail10 >= 2 && tail10 <= 4) return forms[1];
  return forms[2];
}

/** Группа из трёх цифр словами. `feminine` — для тысяч («две тысячи»). */
function tripletInWords(n, feminine) {
  const words = [];
  const hundreds = Math.floor(n / 100);
  const rest = n % 100;
  if (hundreds) words.push(HUNDREDS[hundreds]);
  if (rest >= 10 && rest <= 19) {
    words.push(TEENS[rest - 10]);
  } else {
    const tens = Math.floor(rest / 10);
    const ones = rest % 10;
    if (tens) words.push(TENS[tens]);
    if (ones) words.push((feminine ? ONES_F : ONES)[ones]);
  }
  return words;
}

/** Целое число словами: 4954 → «четыре тысячи девятьсот пятьдесят четыре». */
export function numberInWords(value) {
  let n = Math.abs(Math.trunc(Number(value) || 0));
  if (n === 0) return "ноль";
  const groups = [
    { forms: BILLION, feminine: false },
    { forms: MILLION, feminine: false },
    { forms: THOUSAND, feminine: true },
    { forms: null, feminine: false }, // единицы — без своего слова
  ];
  const parts = [];
  const divisors = [1e9, 1e6, 1e3, 1];
  for (let i = 0; i < divisors.length; i += 1) {
    const chunk = Math.floor(n / divisors[i]);
    n -= chunk * divisors[i];
    if (!chunk) continue;
    parts.push(...tripletInWords(chunk, groups[i].feminine));
    if (groups[i].forms) parts.push(plural(chunk, groups[i].forms));
  }
  return parts.join(" ");
}

/**
 * Сумма прописью для документа: «Четыре тысячи девятьсот пятьдесят четыре сома
 * 00 тыйын». Тыйын оставляем цифрами — так их пишут и в 1С, и в банке.
 */
export function amountInWords(value) {
  const amount = Math.abs(Number(value) || 0);
  const soms = Math.floor(amount + 1e-9);
  // Копейки округляем, а не отбрасываем: 0.999 в документе — это 1 тыйын, а не 0.
  const tyiyn = Math.round((amount - soms) * 100);
  // Округление вверх могло дать «100 тыйын» — переносим в сомы.
  const carried = tyiyn === 100 ? soms + 1 : soms;
  const cents = tyiyn === 100 ? 0 : tyiyn;
  const words = numberInWords(carried);
  return (
    words.charAt(0).toUpperCase() +
    words.slice(1) +
    ` ${plural(carried, SOM)} ${String(cents).padStart(2, "0")} тыйын`
  );
}

export default amountInWords;
