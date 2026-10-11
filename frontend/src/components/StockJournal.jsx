/**
 * Лента «Движение» — журнал складских операций.
 *
 * Показывает ВСЕ движения вперемешку по дате: приход, продажу, возврат,
 * списание, инвентаризацию. Продаж больше всего (каждая товарная строка чека —
 * запись), поэтому фильтр по типу стоит первым в панели: без него лента за
 * месяц работы состоит в основном из продаж.
 *
 * Месяц по умолчанию НЕ выбран. Поставки вносят задним числом, и лента,
 * обрезанная текущим месяцем, прятала бы приход ровно в момент его ввода.
 */
import { formatDate, formatMoney } from "../utils/format.js";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { downloadFile } from "../utils/download.js";
import DataTable from "./DataTable.jsx";
import MonthPicker from "./MonthPicker.jsx";
import Field from "./Field.jsx";

const TYPES = ["SUPPLY", "SALE", "RETURN", "WRITE_OFF", "ADJUSTMENT", "CORRECTION", "TRANSFER"];

/** Причина без того, что уже написано в соседних колонках.
 *
 * В журнале причина хранится самодостаточной фразой («Продажа по чеку №13 —
 * форекс 3мм»), потому что её читают и в админке, где колонок нет. В ленте
 * начало этой фразы дублирует колонки «Операция» и «Заказ», и самый широкий
 * столбец таблицы состоял из уже сказанного. Показываем хвост после тире;
 * полная фраза остаётся в подсказке по наведению.
 */
function shortReason(row) {
  const text = row.reason || "";
  if (row.type !== "SALE" && row.type !== "RETURN") return text;
  const dash = text.indexOf(" — ");
  return dash === -1 ? "" : text.slice(dash + 3);
}

// Приход — зелёным, расход — красным: цифру со знаком глазами не ищут.
const TONE = {
  SUPPLY: "ok",
  SALE: "red",
  RETURN: "blue",
  WRITE_OFF: "amber",
  ADJUSTMENT: "",
  // Исправление прихода — запись «было → стало» без движения (D-70).
  CORRECTION: "blue",
};

export default function StockJournal() {
  const { t, i18n } = useTranslation();
  const [rows, setRows] = useState([]);
  const [count, setCount] = useState(0);
  const [page, setPage] = useState(1);
  const [type, setType] = useState("");
  const [material, setMaterial] = useState("");
  const [period, setPeriod] = useState({ year: new Date().getFullYear(), month: null });
  const [materials, setMaterials] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .get("/warehouse/materials/", { params: { ordering: "name", page_size: 200 } })
      .then((r) => setMaterials(r.data.results));
  }, []);

  useEffect(() => {
    setLoading(true);
    const params = { page };
    if (type) params.type = type;
    if (material) params.material = material;
    // Год без месяца не фильтрует ничего: «все месяцы» — это вся история.
    if (period.month) {
      params.year = period.year;
      params.month = period.month;
    }
    api
      .get("/warehouse/inventory-logs/", { params })
      .then((r) => {
        setRows(r.data.results);
        setCount(r.data.count);
      })
      .finally(() => setLoading(false));
  }, [page, type, material, period]);

  // Смена фильтра возвращает на первую страницу — иначе «страница 4» уводит в
  // пустоту, когда после фильтра осталось десять строк.
  const filter = (setter) => (value) => {
    setter(value);
    setPage(1);
  };

  const pageSize = 25;
  const pages = Math.max(1, Math.ceil(count / pageSize));
  const fmtDate = (iso) => formatDate(iso);

  const columns = [
    {
      key: "happened_at",
      label: t("journal.date"),
      render: (r) => fmtDate(r.happened_at),
    },
    {
      key: "type",
      label: t("journal.type"),
      render: (r) => (
        r.supplier_return
          ? <span className="badge amber">{t("journal.supplierReturn")}</span>
          : <span className={`badge ${TONE[r.type] || ""}`}>{t(`logType.${r.type}`, r.type_display)}</span>
      ),
    },
    { key: "material_name", label: t("checkout.material") },
    {
      key: "quantity_changed",
      label: t("journal.change"),
      render: (r) => {
        // У рулона показываем МЕТРЫ: в цехе отрезали 5 пог.м, а склад считает
        // площадь, и «−8 кв.м» отвечает не на тот вопрос. Метры записаны в
        // самой операции (ширина партии известна только там), поэтому берём их,
        // а не делим площадь на ширину из карточки — партии бывают разной
        // ширины, и деление врало бы.
        const metres = r.metres_changed == null ? null : Number(r.metres_changed);
        const value = metres ?? Number(r.quantity_changed);
        const unit = metres == null ? t(`unit.${r.material_unit}`) : t("unit.METER");
        // Исправление прихода движения не несёт: сам приход поправлен на
        // месте, а «было → стало» — в причине. Возврат поставщику (RU-N23) —
        // строка с минусом: приход над ним остаётся как в бумаге.
        if (r.type === "CORRECTION" && !Number(r.quantity_changed)) return <span className="muted">—</span>;
        return (
          <strong
            style={{
              color: value < 0 ? "var(--danger-ink)" : "var(--ok-ink)",
              whiteSpace: "nowrap",  // «−44.65 кв.м» не должно ломаться на две строки
            }}
          >
            {value > 0 ? "+" : ""}
            {value.toFixed(2)} {unit}
          </strong>
        );
      },
    },
    // Стоимость движения (себестоимость продажи, списания, возврата
    // поставщику) — только тем, кто видит деньги: складовщику сервер шлёт null.
    {
      key: "cost",
      label: t("journal.cost"),
      render: (r) => (r.cost != null && Number(r.quantity_changed) ? formatMoney(r.cost, { fraction: 2 }) : "—"),
    },
    {
      key: "order_number",
      label: t("journal.order"),
      render: (r) => (r.order_number ? `№${r.order_number}` : "—"),
    },
    {
      key: "reason",
      label: t("journal.reason"),
      render: (r) => (
        <span className="muted journal-reason" title={r.reason || ""}>
          {shortReason(r) || "—"}
        </span>
      ),
    },
    {
      key: "created_by_username",
      label: t("journal.who"),
      render: (r) => r.created_by_username || "—",
    },
  ];

  return (
    <>
      {/* Подписи у всех трёх фильтров: у выбора месяца она есть по своей
          природе, и без подписей у соседей ряд выглядел как случайный набор
          выпадашек разной высоты. */}
      <div className="toolbar" style={{ alignItems: "flex-end" }}>
        <Field style={{ margin: 0 }} label={t("journal.type")}>
          <select value={type} onChange={(e) => filter(setType)(e.target.value)}>
            <option value="">{t("journal.allTypes")}</option>
            {TYPES.map((code) => (
              <option key={code} value={code}>{t(`logType.${code}`)}</option>
            ))}
          </select>
        </Field>
        <Field style={{ margin: 0 }} label={t("checkout.material")}>
          <select value={material} onChange={(e) => filter(setMaterial)(e.target.value)}>
            <option value="">{t("journal.allMaterials")}</option>
            {materials.map((m) => (
              <option key={m.id} value={m.id}>{m.name}</option>
            ))}
          </select>
        </Field>
        <MonthPicker value={period} onChange={filter(setPeriod)} />
        {/* Выгрузка журнала в Excel — с теми же фильтрами (XL-06). */}
        <button
          type="button"
          className="secondary"
          onClick={() => downloadFile("/warehouse/inventory-logs/", {
            export: "csv",
            ...(type ? { type } : {}),
            ...(material ? { material } : {}),
            ...(period.month ? { year: period.year, month: period.month } : {}),
          }, "zhurnal-sklada.csv")}
        >
          {t("stock2.toExcel")}
        </button>
      </div>

      <DataTable columns={columns} rows={rows} empty={loading ? t("common.loading") : t("journal.empty")} />

      {pages > 1 && (
        <div className="toolbar" style={{ marginTop: 14, alignItems: "center" }}>
          <button className="ghost" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            ‹ {t("common.back")}
          </button>
          <span className="muted" style={{ alignSelf: "center" }}>
            {t("journal.pageOf", { page, pages, count })}
          </span>
          <button className="ghost" disabled={page >= pages} onClick={() => setPage(page + 1)}>
            {t("common.next")} ›
          </button>
        </div>
      )}
    </>
  );
}
