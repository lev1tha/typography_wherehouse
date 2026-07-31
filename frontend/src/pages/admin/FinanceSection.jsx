import { useTranslation } from "react-i18next";

import Finance from "./Finance.jsx";
import Expenses from "./Expenses.jsx";
import FixedExpenses from "./FixedExpenses.jsx";
import Salaries from "./Salaries.jsx";

// Единый раздел «Финансы» одной страницей: сверху сводка/отчёт (выручка,
// расходы, прибыль, долг), ниже — списки трат по видам. Постоянные расходы и
// зарплаты ведутся такими же записями, как покупки, поэтому идут рядом и
// выглядят одинаково. Принцип прежний: итоги сверху, списки трат снизу.
export default function FinanceSection() {
  const { t } = useTranslation();
  return (
    <>
      <h1>{t("nav.finance")}</h1>
      <Finance embedded />

      <h2 style={{ marginTop: 28 }}>{t("fixed.title")}</h2>
      <FixedExpenses embedded />

      <h2 style={{ marginTop: 28 }}>{t("salary.title")}</h2>
      <Salaries embedded />

      <h2 style={{ marginTop: 28 }}>{t("finance.tabPurchases")}</h2>
      <Expenses embedded />
    </>
  );
}
