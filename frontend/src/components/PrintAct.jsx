import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useDialog } from "../hooks/useDialog.js";
import Field from "./Field.jsx";
import Icon from "./Icon.jsx";
import PrintHost from "./PrintHost.jsx";
import amountInWords from "../utils/amountInWords.js";
import { isCanceled, useLatest } from "../utils/latest.js";
import { formatDate, formatNumber } from "../utils/format.js";

// Акт сверки взаиморасчётов с клиентом.
//
// Классика 1С: этим документом закрывают спор о долге с юрлицом. Считает его
// СЕРВЕР (`GET /clients/clients/<id>/statement/`, CLI-04): входящее сальдо —
// всё, что было до начала периода, строки — заказы, оплаты, возвраты,
// списания, обороты и исходящее сальдо со знаком. Раньше форма собиралась в
// браузере из карточки клиента и считала входящее сальдо нулём: за квартал
// печатала долг 5 963 вместо 19 963, а переплату — без знака.
//
// Знак сальдо: плюс — клиент должен нам, минус — мы должны клиенту (аванс,
// сдача, переплата). Кабинет клиента открывает ту же форму со своим адресом
// (`endpoint="/customer/statement/"`).

const money = (n) => formatNumber(n, { min: 2, max: 2 });
const day = (iso) => (iso ? formatDate(iso) : "");
const iso = (d) => d.toLocaleDateString("sv-SE");

// Быстрые периоды: бухгалтер просит «акт за квартал» чаще, чем за «с даты по дату».
function presetRange(key) {
  const now = new Date();
  const y = now.getFullYear();
  const q = Math.floor(now.getMonth() / 3);
  if (key === "month") return { from: iso(new Date(y, now.getMonth(), 1)), to: iso(now) };
  if (key === "quarter") return { from: iso(new Date(y, q * 3, 1)), to: iso(now) };
  if (key === "prevQuarter") {
    const start = new Date(y, q * 3 - 3, 1);
    return { from: iso(start), to: iso(new Date(start.getFullYear(), start.getMonth() + 3, 0)) };
  }
  if (key === "year") return { from: iso(new Date(y, 0, 1)), to: iso(now) };
  return { from: "", to: "" };
}

export default function PrintAct({ client, onClose, endpoint, initialPreset = "quarter" }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.resolvedLanguage;
  const { dialogProps, titleId } = useDialog({ onClose, guardInput: false });
  const init = presetRange(initialPreset);
  const [from, setFrom] = useState(init.from);
  const [to, setTo] = useState(init.to);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const next = useLatest();
  const url = endpoint || `/clients/clients/${client.id}/statement/`;

  useEffect(() => {
    setError("");
    api
      .get(url, {
        params: { ...(from ? { date_from: from } : {}), ...(to ? { date_to: to } : {}) },
        signal: next(),
      })
      .then((r) => setData(r.data))
      .catch((e) => {
        if (isCanceled(e)) return;
        setData(null);
        setError(apiError(e, t("common.loadFailed")));
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, from, to]);

  // Подпись строки по её виду: сервер отдаёт `kind`, текст — по языку интерфейса.
  const docOf = (r) => {
    const n = r.order_number;
    const title = r.title ? ` «${r.title}»` : "";
    const method = r.method_display ? ` · ${r.method_display}` : "";
    switch (r.kind) {
      case "order": return `${t("print.actOrder", { n })}${title}`;
      case "paid_upfront": return t("print.actPaidUpfront", { n });
      case "payment": return `${t("print.actPaymentFor", { n })}${method}`;
      case "offset": return t("print.actOffset", { n });
      case "change_applied": return t("print.actChangeApplied", { n });
      case "refund": return t("print.actRefund", { n });
      case "refund_paid": return t("print.actRefundPaid", { n });
      case "write_off": return t("print.actWriteOff", { n });
      // Отмена оплаты / списания по заказу (D-155, D-158) — дебетом днём отмены.
      case "payment_cancelled": return `${t("print.actPaymentCancelled", { n })}${method}`;
      case "write_off_cancelled": return t("print.actWriteOffCancelled", { n });
      case "change_given": return t("print.actChangeGiven", { n });
      case "advance": return `${t("print.actAdvance")}${method}`;
      case "advance_reverted": return t("print.actAdvanceReverted");
      // Входящие остатки на дату переезда из Excel (волна 2).
      case "opening_debt": return t("print.actOpeningDebt");
      case "opening_payment": return `${t("print.actOpeningPayment")}${method}`;
      case "opening_write_off": return t("print.actOpeningWriteOff");
      // Отмена оплаты/списания входящего долга (D-141) — дебетом днём отмены.
      case "opening_payment_cancelled": return `${t("print.actOpeningPaymentCancelled")}${method}`;
      case "opening_write_off_cancelled": return t("print.actOpeningWriteOffCancelled");
      case "opening_advance": return t("print.actOpeningAdvance");
      // Зачёт аванса в заказ (волна 2): сальдо не меняет, сумма — в подписи.
      case "advance_used": return t("print.actAdvanceUsed", { n, sum: money(r.amount) });
      default: return t("print.actAdjust");
    }
  };

  const rows = useMemo(() => data?.rows || [], [data]);
  const opening = Number(data?.opening || 0);
  const closing = Number(data?.closing || 0);
  const debit = Number(data?.turnover?.debit || 0);
  const credit = Number(data?.turnover?.credit || 0);

  // Сальдо по сторонам таблицы: долг клиента — слева («начислено»), наш долг
  // перед клиентом — справа («оплачено»).
  const side = (v) => ({ d: v > 0 ? money(v) : "", c: v < 0 ? money(-v) : "" });
  const open = side(opening);

  const who = data?.client || client || {};
  const period = from || to;

  return (
    <PrintHost>
      <div className="modal wide print-modal" {...dialogProps}>
        <div className="modal-head no-print">
          <h2 id={titleId}>{t("print.actTitle")}</h2>
          <button className="ghost" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" size={18} />
          </button>
        </div>

        <div className="row no-print" style={{ gap: 6, marginBottom: 10, flexWrap: "wrap" }}>
          {["quarter", "prevQuarter", "month", "year", "all"].map((k) => {
            const r = presetRange(k);
            const active = r.from === from && r.to === to;
            return (
              <button
                key={k}
                type="button"
                className={active ? "" : "secondary"}
                style={{ padding: "4px 12px", height: "auto", fontSize: 13 }}
                onClick={() => { setFrom(r.from); setTo(r.to); }}
              >
                {t(`print.actPreset_${k}`)}
              </button>
            );
          })}
        </div>
        <div className="row no-print" style={{ alignItems: "flex-end", gap: 10, marginBottom: 14 }}>
          <Field style={{ margin: 0 }} label={t("dashboard.from")}>
            <input type="date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} />
          </Field>
          <Field style={{ margin: 0 }} label={t("dashboard.to")}>
            <input type="date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} />
          </Field>
        </div>

        {error && <p className="field-error no-print" role="alert">{error}</p>}
        {!data && !error && <p className="muted no-print">{t("common.loading")}</p>}

        {/* Без шапки и без строки «Поставщик»: реквизиты организации из
            системы убраны по просьбе заказчика. Акт от этого не ломается —
            он про расчёты с конкретным клиентом. */}
        {data && (
          <div className="print-sheet">
            <h2 className="doc-title">
              {t("print.actHeading")}
              {period && (
                <>
                  {" "}
                  {t("print.actPeriod")} {from ? day(from) : "…"} — {to ? day(to) : day(new Date())}
                </>
              )}
            </h2>

            <p className="doc-line">{t("print.actIntro")}</p>
            <p className="doc-line">
              <b>{t("print.buyer")}:</b> {who.display_name}
              {who.inn ? `, ${t("print.inn")} ${who.inn}` : ""}
              {who.phone ? `, ${t("print.tel")} ${who.phone}` : ""}
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
                  <td className="r">{open.d || (opening === 0 ? "0,00" : "")}</td>
                  <td className="r">{open.c || (opening === 0 ? "0,00" : "")}</td>
                </tr>
                {rows.map((r, i) => (
                  <tr key={`${r.date}-${i}`}>
                    <td>{day(r.date)}</td>
                    <td>{docOf(r)}</td>
                    <td className="r">{Number(r.debit) ? money(r.debit) : ""}</td>
                    <td className="r">{Number(r.credit) ? money(r.credit) : ""}</td>
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
                <tr>
                  <td colSpan={2}><b>{t("print.actClosing")}</b></td>
                  <td className="r"><b>{closing > 0 ? money(closing) : ""}</b></td>
                  <td className="r"><b>{closing < 0 ? money(-closing) : ""}</b></td>
                </tr>
              </tbody>
            </table>

            {/* Итог словами, со стороны: долг клиента или наш долг (аванс).
                Раньше переплата печаталась как «1 300,00 сом» и тут же
                «задолженности нет» — без знака и без объяснения. */}
            <p className="doc-line">
              {closing > 0 && (
                <>
                  {t("print.actOwes", { sum: money(closing) })}
                  <br />
                  <b>{t("print.inWords")}:</b> {amountInWords(closing, lang)}
                </>
              )}
              {closing < 0 && (
                <>
                  {t("print.actOwed", { sum: money(-closing) })}
                  <br />
                  <b>{t("print.inWords")}:</b> {amountInWords(-closing, lang)}
                </>
              )}
              {closing === 0 && t("print.actClear")}
            </p>

            <div className="doc-signs">
              <div>
                <span>{t("print.actFromUs")}</span>
                <span className="doc-rule" />
                <em />
              </div>
              <div>
                <span>{t("print.actFromClient")}</span>
                <span className="doc-rule" />
                <em />
              </div>
            </div>
          </div>
        )}

        <div className="row no-print" style={{ marginTop: 16 }}>
          <button className="secondary" onClick={onClose}>{t("common.close")}</button>
          <button onClick={() => window.print()} disabled={!data}>
            <Icon name="printer" size={16} /> {t("print.print")}
          </button>
        </div>
      </div>
    </PrintHost>
  );
}
