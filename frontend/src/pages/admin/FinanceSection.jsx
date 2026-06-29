import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import Finance from "./Finance.jsx";
import Expenses from "./Expenses.jsx";

// Единый раздел «Финансы»: «Отчёт» (P&L = Finance) и «Покупки» (расходы/
// инвестиции = Expenses). Вкладка хранится в URL (?tab=purchases), чтобы на
// «Покупки» можно было попасть прямой ссылкой.
export default function FinanceSection() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "purchases" ? "purchases" : "report";
  const setTab = (key) =>
    setParams(key === "purchases" ? { tab: "purchases" } : {}, { replace: true });

  return (
    <>
      <h1>{t("nav.finance")}</h1>
      <div className="tabs">
        <button className={tab === "report" ? "active" : ""} onClick={() => setTab("report")}>
          {t("finance.tabReport")}
        </button>
        <button className={tab === "purchases" ? "active" : ""} onClick={() => setTab("purchases")}>
          {t("finance.tabPurchases")}
        </button>
      </div>
      {tab === "report" ? <Finance embedded /> : <Expenses embedded />}
    </>
  );
}
