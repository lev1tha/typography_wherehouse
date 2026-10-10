import { useTranslation } from "react-i18next";

import { isCutLine } from "../utils/itemLabel.js";
import { formatDate, formatNumber } from "../utils/format.js";

// «Наряд мастеру» (XL-11): лист для цеха, а не для клиента.
//
// В товарном чеке и накладной размеры деталей, станок и материал работы — мелкая
// подпись под названием; мастеру же нужно ровно это, а цена не нужна вовсе. Поэтому
// здесь НЕТ ни цен, ни сумм, ни скидок, ни срочной наценки — только «что резать,
// сколько, на чём и по какому материалу». Лист можно отдать на станок, не думая о
// том, что в нём лишнего видит чужой глаз.
//
// Компонент рисует только содержимое листа: рамка окна, вкладки и кнопка печати —
// в PrintDocs, как и у остальных печатных форм.
const qty = (n) => formatNumber(n, { max: 3 });
const dim = (n) => formatNumber(n, { max: 3 });

// Режим продажи материала. Штучный материал (крепёж, клей) тоже хранится как
// «по площади», поэтому подпись ставим только там, где она точна: у листа
// целиком, у рулона и у листового материала, проданного по площади.
function saleModeKey(it) {
  if (it.type !== "MATERIAL") return null;
  if (it.sale_mode === "PIECE") return "PIECE";
  if (it.sale_mode === "METER") return "METER";
  if (it.sale_mode === "SQM" && it.unit_code === "SQM") return "SQM";
  return null;
}

export default function PrintWorkOrder({ receipt, items }) {
  const { t } = useTranslation();
  const number = receipt.order_number;
  const date = receipt.created_at ? formatDate(receipt.created_at) : "";
  const who = receipt.client_name || receipt.buyer_name || t("print.buyerWalkIn");

  return (
    <>
      <h2 className="doc-title">
        {t("print.docHead", { title: t("workOrder.title"), number, date })}
      </h2>

      {receipt.is_urgent && <p className="doc-urgent">{t("workOrder.urgent")}</p>}

      <p className="doc-line"><b>{t("workOrder.client")}:</b> {who}</p>
      {receipt.title && (
        <p className="doc-line"><b>{t("workOrder.orderName")}:</b> {receipt.title}</p>
      )}
      {/* Переделка по гарантии: мастеру важно знать, что именно не так вышло в
          прошлый раз. Деньги (стоимость переделки) на листе не показываем. */}
      {receipt.is_warranty && (
        <p className="doc-line">
          <b>{t("workOrder.warranty", { n: receipt.warranty_of_number ?? "—" })}</b>
          {receipt.warranty_reason ? `: ${receipt.warranty_reason}` : ""}
        </p>
      )}

      <table className="doc-table">
        <thead>
          <tr>
            <th style={{ width: "5%" }}>№</th>
            <th>{t("workOrder.colName")}</th>
            <th style={{ width: "15%" }}>{t("workOrder.colSize")}</th>
            <th className="r" style={{ width: "8%" }}>{t("workOrder.colParts")}</th>
            <th className="r" style={{ width: "10%" }}>{t("workOrder.colRunM")}</th>
            <th className="r" style={{ width: "9%" }}>{t("workOrder.colPasses")}</th>
            <th className="r" style={{ width: "14%" }}>{t("workOrder.colQty")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it, i) => {
            const cut = isCutLine(it);
            const name = (it.type === "SERVICE" ? it.service_name : it.material_name) || "—";
            const unit = it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || "";
            const mode = saleModeKey(it);
            const hasSize = Number(it.width) > 0 && Number(it.length) > 0;
            // Станок и материал работы — подписью под названием, не отдельными
            // колонками: у большинства строк их нет, и пустые колонки съели бы
            // ширину у размеров.
            const machine = it.machine
              ? t(`machine.${it.machine}`, { defaultValue: it.machine_display || it.machine })
              : "";
            const details = [
              machine && `${t("workOrder.machine")}: ${machine}`,
              it.work_material_name && `${t("workOrder.material")}: ${it.work_material_name}`,
              it.executor_name && `${t("checkout2.executor")}: ${it.executor_name}`,
            ].filter(Boolean);
            return (
              <tr key={it.id}>
                <td className="c">{i + 1}</td>
                <td>
                  <b>{name}</b>
                  {it.own_material && <span className="doc-sub">{t("checkout.ownCutTag")}</span>}
                  {it.note && <span className="doc-sub">{t("workOrder.note")}: {it.note}</span>}
                  {details.length > 0 && <span className="doc-sub">{details.join(" · ")}</span>}
                </td>
                <td>{hasSize ? t("receiptsV2.spec.size", { w: dim(it.width), l: dim(it.length) }) : "—"}</td>
                <td className="r">{Number(it.parts_count) > 1 || hasSize ? it.parts_count || 1 : "—"}</td>
                <td className="r">{cut && Number(it.quantity) > 0 ? qty(it.quantity) : "—"}</td>
                <td className="r">{Number(it.passes) > 1 ? it.passes : hasSize ? 1 : "—"}</td>
                <td className="r">
                  {/* У резки количество строки — метры реза, они в своей колонке. */}
                  {cut ? "—" : `${qty(it.quantity)} ${unit}`.trim()}
                  {mode && <span className="doc-sub">{t(`workOrder.mode.${mode}`)}</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="doc-signs">
        <div>
          <span>{t("workOrder.executor")}</span>
          <span className="doc-rule" />
        </div>
        <div>
          <span>{t("workOrder.doneOn")}</span>
          <span className="doc-rule" />
        </div>
      </div>
    </>
  );
}
