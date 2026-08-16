import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Icon from "./Icon.jsx";
import PrintHost from "./PrintHost.jsx";
import amountInWords, { plural } from "../utils/amountInWords.js";

// Печатная форма приходной накладной.
//
// Бумага поставщика остаётся у поставщика; цеху нужен свой лист — тот, который
// подписывает принимающий и который ложится в папку рядом с ней. Поэтому здесь
// же напечатана и сверка: сколько по бумаге, сколько приняли на самом деле и
// на сколько разошлось. Именно этот лист потом показывают поставщику, когда
// спорят о недостаче.

const money = (n) => Number(n || 0).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qty = (n) => Number(n || 0).toLocaleString("ru-RU", { maximumFractionDigits: 3 });
const day = (iso) => (iso ? new Date(iso).toLocaleDateString("ru-RU") : "");

export default function PrintSupply({ supply, onClose }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.resolvedLanguage;
  const [company, setCompany] = useState(null);

  useEffect(() => {
    api.get("/finance/company/").then((r) => setCompany(r.data)).catch(() => setCompany({}));
  }, []);

  if (!company) return null;

  const lines = supply.lines || [];
  const total = Number(supply.total_cost || 0);
  const stated = supply.stated_total == null ? null : Number(supply.stated_total);
  const diff = Number(supply.discrepancy || 0);
  const nameWord = plural(lines.length, [
    t("print.nameOne"), t("print.nameFew"), t("print.nameMany"),
  ]);

  const supplierLine = [
    supply.supplier_name,
    supply.supplier_inn && `${t("print.inn")} ${supply.supplier_inn}`,
    supply.supplier_phone && `${t("print.tel")} ${supply.supplier_phone}`,
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <PrintHost>
      <div className="modal wide print-modal">
        <div className="modal-head no-print">
          <h2>{t("print.docSupply")}</h2>
          <button className="ghost" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" size={18} />
          </button>
        </div>

        <div className="print-sheet">
          <div className="doc-org">
            <strong>{company.name || t("print.noName")}</strong>
            {[company.inn && `${t("print.inn")} ${company.inn}`, company.address, company.phone && `${t("print.tel")} ${company.phone}`]
              .filter(Boolean).length > 0 && (
              <div>
                {[company.inn && `${t("print.inn")} ${company.inn}`, company.address, company.phone && `${t("print.tel")} ${company.phone}`]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
            )}
          </div>

          <h2 className="doc-title">
            {t("print.docHead", {
              title: t("print.docSupply"),
              number: supply.number || `#${supply.id}`,
              date: day(supply.received_on),
            })}
          </h2>

          <p className="doc-line">
            <b>{t("print.supplier")}:</b> {supplierLine || "—"}
          </p>
          <p className="doc-line">
            <b>{t("print.receiver")}:</b> {company.name || "—"}
          </p>
          {supply.note && (
            <p className="doc-line"><b>{t("supplies.note")}:</b> {supply.note}</p>
          )}

          <table className="doc-table">
            <thead>
              <tr>
                <th style={{ width: "6%" }}>№</th>
                <th>{t("print.colName")}</th>
                <th style={{ width: "13%" }}>{t("print.colQty")}</th>
                <th style={{ width: "9%" }}>{t("print.colUnit")}</th>
                <th style={{ width: "15%" }}>{t("supplies.unitCost")}</th>
                <th style={{ width: "16%" }}>{t("print.colSum")}</th>
                <th style={{ width: "14%" }}>{t("supply.rollCode")}</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((l, i) => (
                <tr key={l.id}>
                  <td className="c">{i + 1}</td>
                  <td>{l.material_name}</td>
                  <td className="r">{qty(l.quantity)}</td>
                  <td className="c">{l.unit_code ? t(`unit.${l.unit_code}`) : l.unit}</td>
                  <td className="r">{money(l.unit_cost)}</td>
                  <td className="r">{money(l.cost)}</td>
                  <td className="c">{l.code || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="doc-total">
            <span>{t("print.total")}</span>
            <strong>{money(total)} {t("print.currency")}</strong>
          </div>
          <p className="doc-line">
            {t("print.accepted")} {lines.length} {nameWord} {t("print.forSum")} {money(total)} {t("print.currency")}
          </p>
          <p className="doc-line">
            <b>{t("print.inWords")}:</b> {amountInWords(total, lang)}
          </p>

          {/* Сверка с бумагой поставщика — ради неё лист и печатают. */}
          {stated != null && (
            <table className="doc-bank" style={{ marginTop: 12 }}>
              <tbody>
                <tr>
                  <td>{t("supplies.statedTotal")}</td>
                  <td>{money(stated)} {t("print.currency")}</td>
                </tr>
                <tr>
                  <td>{t("supplies.diff")}</td>
                  <td>
                    {diff === 0
                      ? t("supplies.matches")
                      : `${diff > 0 ? "+" : ""}${money(diff)} ${t("print.currency")}`}
                  </td>
                </tr>
              </tbody>
            </table>
          )}

          {Number(supply.paid_amount) > 0 || Number(supply.debt) > 0 ? (
            <p className="doc-line" style={{ marginTop: 10 }}>
              {t("supplies.paidTo")}: {money(supply.paid_amount)} {t("print.currency")}
              {Number(supply.debt) > 0 && ` · ${t("supplies.debt")}: ${money(supply.debt)} ${t("print.currency")}`}
            </p>
          ) : null}

          <div className="doc-signs">
            <div>
              <span>{t("print.handedBy")}</span>
              <span className="doc-rule" />
              <em />
            </div>
            <div>
              <span>{t("print.acceptedBy")}</span>
              <span className="doc-rule" />
              <em>{supply.created_by_name || ""}</em>
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
