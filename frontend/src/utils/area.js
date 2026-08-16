// Площадь куска — ширина × длина, округлённая до трёх знаков «половиной вверх».
//
// Ровно так же её считает и хранит сервер (`TransactionItem.quantity` —
// DecimalField с тремя знаками, `sale_service` квантует площадь до 0.001,
// ROUND_HALF_UP). Раньше касса брала `(w * l).toFixed(3)`: для 0.45 × 1.23 в
// double получается 0.55349999…, toFixed давал 0.553, а сервер — 0.554. Касса
// показывала 978, чек приходил на 979, «Вся сумма» оставляла долг в 1 сом.
//
// Считаем через целые числа по десятичной записи ввода — без двоичного шума:
// "0.45" × "1.23" → 45 × 123 = 5535 при масштабе 10⁴ → 0.554 при 10³.

function parseScaled(value) {
  const s = String(value ?? "").trim().replace(",", ".");
  if (!/^-?\d*\.?\d*$/.test(s) || s === "" || s === "." || s === "-") return null;
  const neg = s.startsWith("-");
  const [intPart, fracPart = ""] = s.replace("-", "").split(".");
  const digits = (intPart || "0") + fracPart;
  if (!/^\d+$/.test(digits)) return null;
  const n = BigInt(digits) * (neg ? -1n : 1n);
  return { n, scale: fracPart.length };
}

export function areaOf(width, length, decimals = 3) {
  const a = parseScaled(width);
  const b = parseScaled(length);
  if (!a || !b) {
    const f = Number(width) * Number(length);
    if (!Number.isFinite(f)) return 0;
    const k = 10 ** decimals;
    return Math.round(f * k + 1e-7) / k;
  }
  let prod = a.n * b.n;
  let scale = a.scale + b.scale;
  if (scale > decimals) {
    const drop = 10n ** BigInt(scale - decimals);
    // половина вверх (как ROUND_HALF_UP у Decimal), знак учитываем отдельно
    const sign = prod < 0n ? -1n : 1n;
    prod = ((prod * sign + drop / 2n) / drop) * sign;
    scale = decimals;
  }
  return Number(prod) / 10 ** scale;
}
