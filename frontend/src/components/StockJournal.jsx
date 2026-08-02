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
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import DataTable from "./DataTable.jsx";
import MonthPicker from "./MonthPicker.jsx";

const TYPES = ["SUPPLY", "SALE", "RETURN", "WRITE_OFF", "ADJUSTMENT"];

// Приход — зелёным, расход — красным: цифру со знаком глазами не ищут.
const TONE = {
  SUPPLY: "ok",
  SALE: "red",
  RETURN: "blue",
  WRITE_OFF: "amber",
  ADJUSTMENT: "",
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
  const fmtDate = (iso) =>
    new Date(iso).toLocaleDateString(i18n.language, { day: "2-digit", month: "2-digit", year: "numeric" });

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
        <span className={`badge ${TONE[r.type] || ""}`}>{t(`logType.${r.type}`, r.type_display)}</span>
      ),
    },
    { key: "material_name", label: t("checkout.material") },
    {
      key: "quantity_changed",
      label: t("journal.change"),
      render: (r) => {
        const value = Number(r.quantity_changed);
        return (
          <strong
            style={{
              color: value < 0 ? "var(--danger)" : "var(--ok)",
              whiteSpace: "nowrap",  // «−44.65 кв.м» не должно ломаться на две строки
            }}
          >
            {value > 0 ? "+" : ""}
            {value.toFixed(2)} {t(`unit.${r.material_unit}`)}
          </strong>
        );
      },
    },
    {
      key: "order_number",
      label: t("journal.order"),
      render: (r) => (r.order_number ? `№${r.order_number}` : "—"),
    },
    {
      key: "reason",
      label: t("journal.reason"),
      render: (r) => <span className="muted">{r.reason || "—"}</span>,
    },
    {
      key: "created_by_username",
      label: t("journal.who"),
      render: (r) => r.created_by_username || "—",
    },
  ];

  return (
    <>
      {/* flex-end: у выбора месяца сверху подпись, без выравнивания по низу
          селекторы встают лесенкой. */}
      <div className="toolbar" style={{ alignItems: "flex-end" }}>
        <select value={type} onChange={(e) => filter(setType)(e.target.value)}>
          <option value="">{t("journal.allTypes")}</option>
          {TYPES.map((code) => (
            <option key={code} value={code}>{t(`logType.${code}`)}</option>
          ))}
        </select>
        <select value={material} onChange={(e) => filter(setMaterial)(e.target.value)}>
          <option value="">{t("journal.allMaterials")}</option>
          {materials.map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </select>
        <MonthPicker value={period} onChange={filter(setPeriod)} />
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
