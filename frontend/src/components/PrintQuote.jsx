import { useTranslation } from "react-i18next";

import { useDialog } from "../hooks/useDialog.js";
import Icon from "./Icon.jsx";
import PrintHost from "./PrintHost.jsx";
import amountInWords from "../utils/amountInWords.js";
import { itemTitle } from "../utils/itemLabel.js";
import { formatDate, formatNumber } from "../utils/format.js";

// Печатная форма «Коммерческое предложение» (CALC-03). Это НЕ чек: у неё свой
// номер КП, нет «оплачено» и «долг», зато есть срок действия цен и подпись
// продавца. Склад, кассу и выручку КП не трогает — печатается то, что
// сохранено в самом КП (позиции в форме позиций чека), а не пересчитывается.
//
// Печать — тем же механизмом, что остальные формы: окно в портале (`PrintHost`),
// лист А4 `.print-sheet`, `window.print()`.

const money = (n) => formatNumber(n, { min: 2, max: 2 });
const qty = (n) => formatNumber(n, { max: 3 });
const dim = (n) => formatNumber(n, { max: 3 });

/** Подпись размеров строки: «0,33×0,37 м · 12 дет.» — чтобы клиент видел, за что платит. */
function sizeLine(it, t) {
  const bits = [];
  if (it.width && it.length) {
    const one = `${dim(it.width)}×${dim(it.length)} ${t("quote.metresShort")}`;
    bits.push(Number(it.parts_count) > 1 ? `${it.parts_count} × ${one}` : one);
  }
  if (Number(it.passes) > 1) bits.push(`${it.passes} ${t("quote.passesShort")}`);
  if (it.work_material_name && it.type === "SERVICE") bits.push(it.work_material_name);
  return bits.join(" · ");
}

export default function PrintQuote({ quote, onClose }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.resolvedLanguage;
  const { dialogProps, titleId } = useDialog({ onClose, guardInput: false });
  const items = (quote.items || []).filter((i) => !i.is_returned);
  const total = Number(quote.total_price || 0);
  const to = quote.client_label || "";

  return (
    <PrintHost>
      <div className="modal wide print-modal" {...dialogProps}>
        <div className="modal-head no-print">
          <h2 id={titleId}>{t("quote.title")} № {quote.number}</h2>
          <button className="ghost" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" size={18} />
          </button>
        </div>

        <div className="print-sheet">
          <h2 className="doc-title">
            {t("quote.docHead", { number: quote.number, date: formatDate(quote.created_at) })}
          </h2>
          {to && <p className="doc-line"><b>{t("quote.to")}:</b> {to}</p>}
          {quote.title && <p className="doc-line"><b>{t("quote.subject")}:</b> {quote.title}</p>}
          <table className="doc-table">
            <thead>
              <tr>
                <th style={{ width: "6%" }}>№</th>
                <th>{t("print.colName")}</th>
                <th style={{ width: "12%" }}>{t("print.colQty")}</th>
                <th style={{ width: "10%" }}>{t("print.colUnit")}</th>
                <th style={{ width: "16%" }}>{t("print.colPrice")}</th>
                <th style={{ width: "18%" }}>{t("print.colSum")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it, i) => (
                <tr key={it.id ?? i}>
                  <td className="c">{i + 1}</td>
                  <td>
                    {itemTitle(it, t)}
                    {sizeLine(it, t) && <div className="doc-sub">{sizeLine(it, t)}</div>}
                  </td>
                  <td className="r">{qty(it.quantity)}</td>
                  <td className="c">{it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label}</td>
                  <td className="r">{money(it.price_per_item)}</td>
                  <td className="r">{money(it.line_total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="doc-total">
            <span>{t("print.total")}</span>
            <strong>{money(total)} {t("print.currency")}</strong>
          </div>
          <p className="doc-line">
            <b>{t("print.inWords")}:</b> {amountInWords(total, lang)}
          </p>
          {quote.valid_until && (
            <p className="doc-note">{t("quote.validUntil", { date: formatDate(quote.valid_until) })}</p>
          )}
          {quote.is_urgent && <p className="doc-note">{t("quote.urgentNote")}</p>}
          {quote.note && <p className="doc-note">{quote.note}</p>}
          <div className="doc-signs">
            <div>
              <span>{t("quote.seller")}</span>
              <span className="doc-rule" />
              <em>{quote.created_by_name || ""}</em>
            </div>
          </div>
        </div>

        <div className="row no-print" style={{ marginTop: 16 }}>
          <button className="secondary" onClick={onClose}>{t("common.close")}</button>
          <button onClick={() => window.print()}>
            <Icon name="printer" size={16} /> {t("print.print")}
          </button>
        </div>
      </div>
    </PrintHost>
  );
}
