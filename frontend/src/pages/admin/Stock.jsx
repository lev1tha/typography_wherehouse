import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import Catalog from "./Catalog.jsx";
import MaterialStock from "./MaterialStock.jsx";
import Supply from "../store/Supply.jsx";

const TABS = ["materials", "movement", "sheet"];

// Единый раздел «Склад»: «Материалы» (справочник = Catalog), «Движение»
// (приход / инвентаризация / списание = Supply) и «Остатки по месяцам» —
// складской лист заказчика из Excel. Вкладка хранится в URL (?tab=movement),
// чтобы на неё можно было попасть прямой ссылкой.
export default function Stock() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab = TABS.includes(raw) ? raw : "materials";
  const setTab = (key) => setParams(key === "materials" ? {} : { tab: key }, { replace: true });

  const label = { materials: "tabMaterials", movement: "tabMovement", sheet: "tabSheet" };

  return (
    <>
      <h1>{t("warehouse.title")}</h1>
      <div className="tabs">
        {TABS.map((key) => (
          <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>
            {t(`warehouse.${label[key]}`)}
          </button>
        ))}
      </div>
      {tab === "materials" && <Catalog embedded />}
      {tab === "movement" && <Supply embedded />}
      {tab === "sheet" && <MaterialStock embedded />}
    </>
  );
}
