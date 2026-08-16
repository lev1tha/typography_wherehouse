import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Icon from "./Icon.jsx";
import PrintHost from "./PrintHost.jsx";
import amountInWords from "../utils/amountInWords.js";

// Акт сверки взаиморасчётов с клиентом.
//
// Классика 1С: этим документом закрывают спор о долге с юрлицом. Данные для
// него в системе уже были — заказы и оплаты лежат в карточке клиента, — не
// хватало только формы. Поэтому здесь нет ни одного нового запроса к серверу
// сверх того, что карточка и так грузит.
//
// Сальдо считаем от НУЛЯ на начало периода: система не ведёт входящих остатков
// по клиентам, и придумывать их нельзя. Поэтому за период по умолчанию берём
// всю историю — тогда сальдо на конец совпадает с настоящим долгом клиента.

const money = (n) => Number(n || 0).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const day = (iso) => (iso ? new Date(iso).toLocaleDateString("ru-RU") : "");

export default function PrintAct({ client, onClose }) {
  const { t } = useTranslation();
  const [company, setCompany] = useState(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  useEffect(() => {
    api.get("/finance/company/").then((r) => setCompany(r.data)).catch(() => setCompany({}));
  }, []);

  // Строки акта: заказ — начисление, оплата — погашение. Обе стороны в одной
  // ленте по датам, как в 1С.
  //
  // Тонкость: оплата, принятая ПРЯМО В КАССЕ при оформлении, записи
  // `sales.Payment` не создаёт — та заводится только при погашении долга.
  // Поэтому у строки заказа своя колонка «оплачено»: это `amount_paid` минус
  // всё, что позже пришло отдельными платежами. Иначе акт показывал бы все
  // заказы неоплаченными, а сальдо — вдвое больше настоящего долга.
  //
  // ВОЗВРАТ — тоже строка акта, иначе сальдо врёт: заказ вернули, долг в
  // карточке 0, а акт продолжал показывать «начислено 226 — задолженность 226».
  // Возврат товара уменьшает начисленное (идёт в «оплачено» как погашение
  // возвратом), а деньги, выданные клиенту обратно из кассы, — обратно в
  // «начислено»: он их получил, значит, снова должен ровно на эту сумму
  // меньше «оплатил». Дата у обеих строк — дата заказа: так же возвраты
  // относит к заказу вся отчётность (выручка месяца минус возвраты по его
  // заказам), и акт за период не расходится с ней.
  const rows = useMemo(() => {
    const inRange = (d) => (!from || d >= from) && (!to || d <= to);
    const laterPaid = {};
    for (const p of client.payments || []) {
      laterPaid[p.order_number] = (laterPaid[p.order_number] || 0) + Number(p.amount || 0);
    }
    const orders = [];
    for (const o of client.orders || []) {
      const date = String(o.created_at).slice(0, 10);
      const n = o.order_number;
      const paid = Number(o.amount_paid || 0);
      const refunded = Number(o.refunded_amount || 0);
      const upfront = paid - (laterPaid[n] || 0);
      orders.push({
        date,
        seq: [n, 0],
        doc: t("print.actOrder", { n }),
        debit: Number(o.total_price || 0),
        // Отрицательным не бывает, но округления в данных лучше не тащить в акт.
        credit: Math.max(0, upfront),
      });
      if (refunded > 0) {
        orders.push({ date, seq: [n, 1], doc: t("print.actRefund", { n }), debit: 0, credit: refunded });
        // Из кассы отдают не больше, чем по заказу приняли (так пишет и
        // кассовая книга: `min(возврат, оплачено)`).
        const paidBack = Math.min(refunded, paid);
        if (paidBack > 0) {
          orders.push({ date, seq: [n, 2], doc: t("print.actRefundPaid", { n }), debit: paidBack, credit: 0 });
        }
      }
    }
    const payments = (client.payments || []).map((p) => ({
      date: String(p.paid_on).slice(0, 10),
      seq: [p.order_number, 3, p.id],
      doc: `${t("print.actPayment")} №${p.order_number}${p.method_display ? ` · ${p.method_display}` : ""}`,
      debit: 0,
      credit: Number(p.amount || 0),
    }));
    // По датам, внутри дня — по номеру заказа, внутри заказа — заказ, возврат,
    // выдача, оплаты по порядку: раньше одинаковые даты ложились как попало.
    const cmp = (a, b) => {
      if (a.date !== b.date) return a.date.localeCompare(b.date);
      for (let i = 0; i < Math.max(a.seq.length, b.seq.length); i++) {
        const d = (a.seq[i] ?? 0) - (b.seq[i] ?? 0);
        if (d) return d;
      }
      return 0;
    };
    return [...orders, ...payments].filter((r) => inRange(r.date)).sort(cmp);
  }, [client.orders, client.payments, from, to, t]);

  const debit = rows.reduce((s, r) => s + r.debit, 0);
  const credit = rows.reduce((s, r) => s + r.credit, 0);
  const closing = debit - credit;

  if (!company) return null;

  return (
    <PrintHost>
      <div className="modal wide print-modal">
        <div className="modal-head no-print">
          <h2>{t("print.actTitle")}</h2>
          <button className="ghost" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" size={18} />
          </button>
        </div>

        <div className="row no-print" style={{ alignItems: "flex-end", gap: 10, marginBottom: 14 }}>
          <div className="field" style={{ margin: 0 }}>
            <label>{t("dashboard.from")}</label>
            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </div>
          <div className="field" style={{ margin: 0 }}>
            <label>{t("dashboard.to")}</label>
            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </div>
          {(from || to) && (
            <button className="ghost" onClick={() => { setFrom(""); setTo(""); }}>
              {t("common.reset")}
            </button>
          )}
        </div>

        <div className="print-sheet">
          <div className="doc-org">
            <strong>{company.name || t("print.noName")}</strong>
          </div>
          <h2 className="doc-title">
            {t("print.actHeading")}
            {(from || to) && (
              <>
                {" "}
                {t("print.actPeriod")} {from ? day(from) : "…"} — {to ? day(to) : day(new Date())}
              </>
            )}
          </h2>

          <p className="doc-line">{t("print.actIntro")}</p>
          <p className="doc-line">
            <b>{t("print.supplier")}:</b> {company.name || "—"}
            {company.inn ? `, ИНН ${company.inn}` : ""}
          </p>
          <p className="doc-line">
            <b>{t("print.buyer")}:</b> {client.display_name}
            {client.inn ? `, ИНН ${client.inn}` : ""}
            {client.phone ? `, тел. ${client.phone}` : ""}
          </p>

          <table className="doc-table">
            <thead>
              <tr>
                <th style={{ width: "16%" }}>{t("print.actDate")}</th>
                <th>{t("print.actDoc")}</th>
                <th style={{ width: "20%" }}>{t("print.actDebit")}</th>
                <th style={{ width: "20%" }}>{t("print.actCredit")}</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td colSpan={2}>{t("print.actOpening")}</td>
                <td className="r">0,00</td>
                <td className="r">0,00</td>
              </tr>
              {rows.map((r, i) => (
                <tr key={`${r.date}-${i}`}>
                  <td>{day(r.date)}</td>
                  <td>{r.doc}</td>
                  <td className="r">{r.debit ? money(r.debit) : ""}</td>
                  <td className="r">{r.credit ? money(r.credit) : ""}</td>
                </tr>
              ))}
              {!rows.length && (
                <tr>
                  <td colSpan={4} className="c">{t("print.actEmpty")}</td>
                </tr>
              )}
              <tr>
                <td colSpan={2}><b>{t("print.actTurnover")}</b></td>
                <td className="r"><b>{money(debit)}</b></td>
                <td className="r"><b>{money(credit)}</b></td>
              </tr>
            </tbody>
          </table>

          <div className="doc-total">
            <span>{t("print.actClosing")}</span>
            <strong>{money(Math.abs(closing))} сом</strong>
          </div>
          <p className="doc-line">
            {closing > 0 ? (
              <>
                {t("print.actOwes", { sum: money(closing) })}
                <br />
                <b>{t("print.inWords")}:</b> {amountInWords(closing)}
              </>
            ) : (
              t("print.actClear")
            )}
          </p>

          <div className="doc-signs">
            <div>
              <span>{t("print.actFromUs")}</span>
              <span className="doc-rule" />
              <em>{company.director || ""}</em>
            </div>
            <div>
              <span>{t("print.actFromClient")}</span>
              <span className="doc-rule" />
              <em />
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
