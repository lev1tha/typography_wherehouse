// Проверка CSS-переменных: каждая `var(--имя)` в стилях и в JSX должна быть где-то
// объявлена (`--имя: значение` в CSS или ключ `"--имя"` в style-объекте).
//
// Зачем: необъявленная переменная не падает и не пишет в консоль — правило
// молча не применяется. Так `.filter-date` рисовалась без рамки (`--line`,
// `--radius-sm`), а кольцо фокуса у «ⓘ» не появлялось (`--accent`). Запуск:
//   npm run lint:css-vars
// Код выхода 1, если нашлись необъявленные — можно повесить на CI/хук.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

// Каталог можно передать аргументом (для проверки самого скрипта).
const SRC = process.argv[2] || join(dirname(fileURLToPath(import.meta.url)), "..", "src");

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(css|jsx?|html)$/.test(name)) out.push(p);
  }
  return out;
}

const files = walk(SRC);
const declared = new Set();
const used = []; // { name, file, line, fallback }

for (const file of files) {
  const text = readFileSync(file, "utf8");
  for (const m of text.matchAll(/(?:^|[\s;{"'`])(--[\w-]+)\s*(?::|"\s*:|'\s*:)/g)) declared.add(m[1]);
  text.split("\n").forEach((line, i) => {
    for (const m of line.matchAll(/var\(\s*(--[\w-]+)\s*(,)?/g)) {
      used.push({ name: m[1], file, line: i + 1, fallback: !!m[2] });
    }
  });
}

const bad = used.filter((u) => !declared.has(u.name));
if (bad.length) {
  console.error(`Необъявленные CSS-переменные: ${bad.length}`);
  for (const b of bad) {
    console.error(`  ${b.name}${b.fallback ? " (с запасным значением)" : ""}  ${relative(SRC, b.file)}:${b.line}`);
  }
  process.exit(1);
}
console.log(`CSS-переменные в порядке: ${new Set(used.map((u) => u.name)).size} имён, ${used.length} использований, необъявленных нет.`);
