import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import Catalog from "./Catalog.jsx";
import Supply from "../store/Supply.jsx";

// Единый раздел «Склад»: «Материалы» (справочник = Catalog) и «Движение»
// (приход / инвентаризация / списание = Supply). Вкладка хранится в URL
// (?tab=movement), чтобы на «Движение» можно было попасть прямой ссылкой.
export default function Stock() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "movement" ? "movement" : "materials";
  const setTab = (key) =>
    setParams(key === "movement" ? { tab: "movement" } : {}, { replace: true });

  return (
    <>
      <h1>{t("warehouse.title")}</h1>
      <div className="tabs">
        <button className={tab === "materials" ? "active" : ""} onClick={() => setTab("materials")}>
          {t("warehouse.tabMaterials")}
        </button>
        <button className={tab === "movement" ? "active" : ""} onClick={() => setTab("movement")}>
          {t("warehouse.tabMovement")}
        </button>
      </div>
      {tab === "materials" ? <Catalog embedded /> : <Supply embedded />}
    </>
  );
}
