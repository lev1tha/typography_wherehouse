import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";
import { issuedLabel, itemSpecParts, itemTitle } from "../utils/itemLabel.js";
import { formatNumber } from "../utils/format.js";

// Выдача по позициям (G1-N4): 8 деталей из 10 вручили в понедельник, две ещё
// дорезают. Раньше заказ был либо «Готовится», либо «Выдан» целиком, и отдать
// часть значило соврать системе в одну из сторон.
//
// Здесь отмечают, сколько отдали СЕЙЧАС. По умолчанию — ничего (перепроверка
// 10.10, RU-N3): раньше окно предлагало выдать всё, и одно нажатие «Выдать»
// отмечало выданным весь заказ; «Всё оставшееся» — кнопкой. У строки с
// деталями выдача в ДЕТАЛЯХ (шт), а не в кв.м или пог.м: клиенту отдают
// таблички, а не 0,3 кв.м. Статус заказа сервер считает сам: всё выдано —
// «Выдан», хоть что-то — «Выдан частично».
const round3 = (n) => Math.round(n * 1000) / 1000;
const trim = (n) => String(+round3(n).toFixed(3));
// Строка с деталями выдаётся штуками.
const byParts = (it) => Number(it.parts_count) > 1;

export default function IssueItemsModal({ receipt, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  // Возвращённые строки выдавать нельзя — клиент их уже принёс обратно.
  const items = useMemo(() => (receipt.items || []).filter((i) => !i.is_returned), [receipt.items]);
  // Осталось выдать: у строки с деталями — деталей, у прочих — в единице строки.
  const left = (it) => {
    if (byParts(it)) {
      if (Number(it.issued_qty || 0) >= Number(it.quantity)) return 0;
      return Math.max(0, Number(it.parts_count) - Number(it.issued_parts || 0));
    }
    return Math.max(0, round3(Number(it.quantity) - Number(it.issued_qty || 0)));
  };
  const [vals, setVals] = useState(() => Object.fromEntries(items.map((i) => [i.id, ""])));
  const [busy, setBusy] = useState(false);

  const bad = (it) => {
    const v = vals[it.id];
    if (v === "" || v == null) return false;
    if (byParts(it) && !Number.isInteger(Number(v))) return true;
    return !(Number(v) >= 0) || Number(v) > left(it) + 1e-9;
  };
  const picked = items.filter((i) => Number(vals[i.id]) > 0);
  const valid = picked.length > 0 && !items.some(bad);

  async function submit() {
    if (!valid) return;
    setBusy(true);
    try {
      const { data } = await api.post(`/sales/receipts/${receipt.id}/issue/`, {
        items: picked.map((i) =>
          byParts(i) ? { id: i.id, parts: Number(vals[i.id]) } : { id: i.id, quantity: Number(vals[i.id]) }
        ),
      });
      toast(data.fulfillment_status === "ISSUED" ? t("issue.doneAll") : t("issue.done"));
      onDone?.(data);
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={t("issue.title", { number: receipt.order_number })}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !valid}>
            {busy ? t("common.loading") : t("issue.submit")}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: -6 }}>{t("issue.hint")}</p>

      <div className="row" style={{ justifyContent: "flex-end", margin: "0 0 6px" }}>
        <button
          type="button"
          className="ghost"
          style={{ padding: 0, height: "auto", color: "var(--accent-ink)" }}
          onClick={() => setVals(Object.fromEntries(items.map((i) => [i.id, trim(left(i))])))}
        >
          {t("issue.all")}
        </button>
        <button
          type="button"
          className="ghost"
          style={{ padding: 0, height: "auto", color: "var(--accent-ink)" }}
          onClick={() => setVals(Object.fromEntries(items.map((i) => [i.id, ""])))}
        >
          {t("issue.none")}
        </button>
      </div>

      {items.map((it) => {
        const rest = left(it);
        const parts = byParts(it);
        const unit = parts ? t("issue.partsUnit") : it.unit_code ? t(`unit.${it.unit_code}`) : it.unit_label || "";
        const total = parts ? Number(it.parts_count) : it.quantity;
        const spec = itemSpecParts(it, t);
        return (
          <div
            key={it.id}
            className="crow"
            style={{ alignItems: "flex-start", borderBottom: "1px solid var(--hairline)", padding: "8px 0" }}
          >
            <span>
              {itemTitle(it, t)}
              {spec.length > 0 && <span className="rc-spec">{spec.join(" · ")}</span>}
              <span className="rc-spec">
                {issuedLabel(it, t) || t("issue.ofTotal", { total: formatNumber(total, { max: 3 }), unit })}
                {rest > 0 && ` · ${t("issue.left", { n: formatNumber(rest, { max: 3 }), unit })}`}
              </span>
            </span>
            {rest > 0 ? (
              <Field
                style={{ margin: 0, width: 120 }}
                label={t("issue.now")}
                error={bad(it) ? t("issue.tooMuch", { n: formatNumber(rest, { max: 3 }) }) : undefined}
              >
                <input
                  type="number"
                  min="0"
                  max={rest}
                  step={parts ? 1 : "any"}
                  inputMode={parts ? "numeric" : "decimal"}
                  placeholder="0"
                  aria-label={`${itemTitle(it, t)} — ${t("issue.now")}`}
                  value={vals[it.id] ?? ""}
                  onChange={(e) => setVals((v) => ({ ...v, [it.id]: e.target.value }))}
                />
              </Field>
            ) : (
              <span className="badge ok">{t("issue.full")}</span>
            )}
          </div>
        );
      })}
      {!items.length && <p className="muted">{t("issue.noLines")}</p>}
      {items.length > 0 && !picked.length && <p className="muted" style={{ fontSize: 12 }}>{t("issue.nothing")}</p>}
    </Modal>
  );
}
