import { useTranslation } from "react-i18next";

import Finance from "./Finance.jsx";
import Expenses from "./Expenses.jsx";

// Единый раздел «Финансы» одной страницей: сверху сводка/отчёт (выручка,
// расходы, прибыль, долг + постоянные расходы), снизу — «Покупки» (переменные
// расходы). Без вкладок — принцип «итоги сверху, список трат снизу».
export default function FinanceSection() {
  const { t } = useTranslation();
  return (
    <>
      <h1>{t("nav.finance")}</h1>
      <Finance embedded />
      <h2 style={{ marginTop: 28 }}>{t("finance.tabPurchases")}</h2>
      <Expenses embedded />
    </>
  );
}
