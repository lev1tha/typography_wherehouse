import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Icon from "./Icon.jsx";
import PrintHost from "./PrintHost.jsx";
import amountInWords, { plural } from "../utils/amountInWords.js";

// Печатные формы заказа: товарный чек и накладная.
//
// Заказчик пришёл из 1С, где печатная форма есть у каждого документа. Здесь её
// не было вообще — ни одной, — и юрлицу нечего было отдать: без накладной оно
// не примет товар.
//
// СЧЁТА НА ОПЛАТУ здесь больше нет: он состоял из реквизитов организации
// (банк, расчётный счёт, БИК, подписи), а реквизиты заказчик из системы убрал.
// Счёт без банка — лист, по которому некуда платить, поэтому вкладка убрана
// целиком, а не оставлена пустой.
//
// Печатаем БРАУЗЕРОМ (`window.print`), без серверной генерации PDF: то же окно
// печати умеет «Сохранить как PDF», а нам не нужны ни шрифты на сервере, ни
// вторая вёрстка тех же таблиц. То, что видно в предпросмотре, и уходит на
// бумагу — предпросмотр свёрстан листом А4.

const money = (n) => Number(n || 0).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qty = (n) => Number(n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 3 });
const day = (iso) => (iso ? new Date(iso).toLocaleDateString("ru-RU") : "");

const DOCS = ["CHECK", "WAYBILL"];

/** Строка «Покупатель» — как её пишут в документе. */
function buyerLine(client, t) {
  if (!client) return t("print.buyerWalkIn");
  const parts = [client.display_name || client.full_name || client.company_name];
  if (client.inn) parts.push(`${t("print.inn")} ${client.inn}`);
  if (client.phone) parts.push(`${t("print.tel")} ${client.phone}`);
  return parts.filter(Boolean).join(", ");
}

function ItemsTable({ items, t }) {
  return (
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
          <tr key={it.id}>
            <td className="c">{i + 1}</td>
            <td>{it.type === "SERVICE" ? it.service_name : it.material_name}</td>
            <td className="r">{qty(it.quantity)}</td>
            <td className="c">{it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label}</td>
            <td className="r">{money(it.price_per_item)}</td>
            <td className="r">{money(it.line_total)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Итог + сумма прописью — общий подвал таблицы у счёта и накладной. */
function TotalBlock({ total, t, lang, summary }) {
  return (
    <>
      <div className="doc-total">
        <span>{t("print.total")}</span>
        <strong>{money(total)} {t("print.currency")}</strong>
      </div>
      <p className="doc-line">{summary}</p>
      <p className="doc-line">
        <b>{t("print.inWords")}:</b> {amountInWords(total, lang)}
      </p>
    </>
  );
}

function SignRow({ left, right, leftName, rightName }) {
  return (
    <div className="doc-signs">
      <div>
        <span>{left}</span>
        <span className="doc-rule" />
        <em>{leftName || ""}</em>
      </div>
      <div>
        <span>{right}</span>
        <span className="doc-rule" />
        <em>{rightName || ""}</em>
      </div>
    </div>
  );
}

export default function PrintDocs({ receipt, onClose }) {
  const { t, i18n } = useTranslation();
  // Язык документа = язык интерфейса: заголовок, шапка таблицы и сумма
  // прописью на одном языке, а не «SALES RECEIPT № 2 ОТ 16.08.2026».
  const lang = i18n.resolvedLanguage;
  const [kind, setKind] = useState("CHECK");
  const [client, setClient] = useState(null);

  useEffect(() => {
    if (receipt.client) {
      api
        .get(`/clients/clients/${receipt.client}/`)
        .then((r) => setClient(r.data))
        .catch(() => setClient(null));
    }
  }, [receipt.client]);

  const items = useMemo(
    () => (receipt.items || []).filter((i) => !i.is_returned),
    [receipt.items]
  );
  const total = Number(receipt.total_price || 0);
  const paid = Number(receipt.amount_paid || 0);
  const debt = Number(receipt.debt || 0);
  const number = receipt.order_number;
  const date = day(receipt.created_at);
  const nameWord = plural(items.length, [
    t("print.nameOne"), t("print.nameFew"), t("print.nameMany"),
  ]);

  const head = (title) => (
    <h2 className="doc-title">
      {t("print.docHead", { title, number, date })}
    </h2>
  );

  return (
    <PrintHost>
      <div className="modal wide print-modal">
        <div className="modal-head no-print">
          <h2>{t("print.title")} № {number}</h2>
          <button className="ghost" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" size={18} />
          </button>
        </div>

        <div className="no-print" style={{ marginBottom: 14 }}>
          <div className="tabs tabs-grid">
            {DOCS.map((d) => (
              <button
                key={d}
                className={kind === d ? "active" : ""}
                onClick={() => setKind(d)}
              >
                {t(`print.doc${d[0]}${d.slice(1).toLowerCase()}`)}
              </button>
            ))}
          </div>
        </div>

        {/* Лист А4: ровно то, что уйдёт на бумагу. */}
        <div className="print-sheet">
          {kind === "CHECK" && (
            <>
              {head(t("print.docCheck"))}
              <ItemsTable items={items} t={t} />
              <div className="doc-total">
                <span>{t("print.total")}</span>
                <strong>{money(total)} {t("print.currency")}</strong>
              </div>
              {paid > 0 && (
                <p className="doc-line">
                  {t("print.paid")}: {money(paid)} {t("print.currency")}
                  {debt > 0 && ` · ${t("print.debt")}: ${money(debt)} ${t("print.currency")}`}
                </p>
              )}
              <p className="doc-line">
                <b>{t("print.inWords")}:</b> {amountInWords(total, lang)}
              </p>
              <SignRow
                left={t("print.seller")}
                right={t("print.buyer")}
                leftName={receipt.cashier_name}
              />
            </>
          )}

          {kind === "WAYBILL" && (
            <>
              {head(t("print.docWaybill"))}
              <p className="doc-line"><b>{t("print.receiver")}:</b> {buyerLine(client, t)}</p>
              {receipt.title && <p className="doc-line"><b>{t("print.basis")}:</b> {receipt.title}</p>}
              <ItemsTable items={items} t={t} />
              <TotalBlock
                total={total}
                t={t}
                lang={lang}
                summary={`${t("print.released")} ${items.length} ${nameWord} ${t("print.forSum")} ${money(total)} ${t("print.currency")}`}
              />
              <SignRow
                left={t("print.handedOver")}
                right={t("print.received")}
              />
            </>
          )}
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
