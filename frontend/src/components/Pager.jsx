import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";

import { formatNumber } from "../utils/format.js";

// Номер страницы, который сам сбрасывается на первую, когда меняются фильтры.
// `filterKey` — любая строка, меняющаяся вместе с фильтрами/сортировкой
// (обычно JSON.stringify списка значений). Сброс считается при чтении, а не в
// эффекте: иначе на смене фильтра уходил бы лишний запрос «старая страница,
// новые фильтры».
export function usePage(filterKey) {
  const [st, setSt] = useState({ key: filterKey, page: 1 });
  const page = st.key === filterKey ? st.page : 1;
  const setPage = useCallback((p) => setSt({ key: filterKey, page: p }), [filterKey]);
  return [page, setPage];
}

// Окно номеров вокруг текущей страницы: 1 … 4 5 [6] 7 8 … 20
function windowed(page, last) {
  const set = new Set([1, last, page, page - 1, page + 1, page - 2, page + 2]);
  const nums = [...set].filter((n) => n >= 1 && n <= last).sort((a, b) => a - b);
  const out = [];
  nums.forEach((n, i) => {
    if (i && n - nums[i - 1] > 1) out.push("gap");
    out.push(n);
  });
  return out;
}

// Постраничный переключатель под списком + «Показано N из M».
// `count` — всего записей по фильтру (поле `count` ответа DRF), `pageSize` —
// размер страницы (по умолчанию 25, как PAGE_SIZE на сервере).
export default function Pager({ page, count, pageSize = 25, onPage }) {
  const { t } = useTranslation();
  const total = Number(count) || 0;
  if (total <= 0) return null;
  const last = Math.max(1, Math.ceil(total / pageSize));
  const from = (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);

  return (
    <nav className="pager" aria-label={t("pager.label")}>
      <span className="pager-info" role="status">
        {t("pager.shown", { from: formatNumber(from), to: formatNumber(to), total: formatNumber(total) })}
      </span>
      {last > 1 && (
        <div className="pager-ctl">
          <button
            type="button"
            className="secondary"
            disabled={page <= 1}
            onClick={() => onPage(page - 1)}
            aria-label={t("pager.prev")}
          >
            ‹
          </button>
          {windowed(page, last).map((n, i) =>
            n === "gap" ? (
              <span key={`g${i}`} className="pager-gap" aria-hidden="true">
                …
              </span>
            ) : (
              <button
                key={n}
                type="button"
                className={n === page ? "" : "secondary"}
                aria-current={n === page ? "page" : undefined}
                aria-label={t("pager.page", { n })}
                onClick={() => n !== page && onPage(n)}
              >
                {n}
              </button>
            )
          )}
          <button
            type="button"
            className="secondary"
            disabled={page >= last}
            onClick={() => onPage(page + 1)}
            aria-label={t("pager.next")}
          >
            ›
          </button>
        </div>
      )}
    </nav>
  );
}
