/**
 * «Перемещение» между площадками (STK-05/G4-N4, волна 2).
 *
 * Раньше перевоз листов в Глобал изображали списанием и новым приходом — в
 * ОПиУ появлялись потеря и закуп, которых не было. Здесь остаток и деньги не
 * меняются: только где лежит. Лист — листами, рулон — метрами с выбранного
 * рулона, штучный — количеством.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { formatNumber } from "../utils/format.js";
import { parseNumber } from "../utils/pasteTable.js";
import Field from "./Field.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

const today = () => new Date().toLocaleDateString("sv-SE");

export default function TransferModal({ material, sites, onClose, onDone }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const bySheet = material.is_roll_material && !material.sells_by_metre && Number(material.piece_area) > 0;
  const byMetre = !!material.sells_by_metre;
  // Откуда по умолчанию — площадка, где лежит больше всего.
  const here = (material.by_site || []).slice().sort((a, b) => Number(b.area) - Number(a.area));
  const [from, setFrom] = useState(here[0]?.site != null ? String(here[0].site) : "");
  const [to, setTo] = useState("");
  const [amount, setAmount] = useState("");
  const [roll, setRoll] = useState("");
  const [rolls, setRolls] = useState([]);
  const [day, setDay] = useState(today());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!byMetre) return;
    api
      .get("/warehouse/rolls/", { params: { material: material.id, page_size: 500 } })
      .then((r) => {
        const list = (r.data.results ?? r.data).filter((x) => Number(x.remaining_area) > 0 && x.width);
        setRolls(list);
        if (list[0]) setRoll(String(list[0].id));
      })
      .catch(() => setRolls([]));
  }, [byMetre, material.id]);

  const qty = parseNumber(amount);
  const unit = byMetre ? t("unit.METER") : bySheet ? t("warehouse.unitSheet") : t(`unit.${material.unit}`);

  async function submit() {
    setBusy(true);
    try {
      await api.post("/warehouse/transfers/", {
        material: material.id,
        from_site: from ? Number(from) : null,
        to_site: to ? Number(to) : null,
        ...(byMetre ? { metres: qty, roll: Number(roll) } : bySheet ? { sheets: qty } : { quantity: qty }),
        happened_on: day || null,
        note,
      });
      toast(t("stock2.transferDone"));
      onDone?.();
      onClose();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusy(false);
    }
  }

  const siteOptions = (
    <>
      <option value="">{t("stock2.noSite")}</option>
      {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
    </>
  );

  return (
    <Modal
      title={`${t("stock2.transfer")}: ${material.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="secondary" onClick={onClose}>{t("common.cancel")}</button>
          <button onClick={submit} disabled={busy || !(qty > 0) || from === to || (byMetre && !roll)}>
            {t("stock2.transfer")}
          </button>
        </>
      }
    >
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>{t("stock2.transferHint")}</p>
      {here.length > 0 && (
        <p style={{ fontSize: 13 }}>
          {here.map((row, i) => (
            <span key={row.site ?? "none"}>
              {i > 0 ? " · " : ""}{row.name || t("stock2.noSite")}: <strong>{formatNumber(row.area, { max: 2 })}</strong>
            </span>
          ))}
        </p>
      )}
      <div className="row">
        <Field className="grow" label={t("stock2.fromSite")}>
          <select value={from} onChange={(e) => setFrom(e.target.value)}>{siteOptions}</select>
        </Field>
        <Field className="grow" label={t("stock2.toSite")}>
          <select value={to} onChange={(e) => setTo(e.target.value)}>{siteOptions}</select>
        </Field>
      </div>
      {byMetre && (
        <Field label={t("supply.writeoffRoll")}>
          <select value={roll} onChange={(e) => setRoll(e.target.value)}>
            {rolls.map((r) => (
              <option key={r.id} value={r.id}>{r.code || `№${r.id}`} · {r.metres_remaining} {t("unit.METER")}</option>
            ))}
          </select>
        </Field>
      )}
      <Field label={`${t("stock2.howMuch")}, ${unit}`}>
        <input type="text" inputMode="decimal" autoFocus value={amount}
               aria-invalid={amount !== "" && qty == null ? "true" : undefined}
               onChange={(e) => setAmount(e.target.value)} />
      </Field>
      <div className="row">
        <Field className="grow" label={t("waste.date")}>
          <input type="date" value={day} max={today()} onChange={(e) => setDay(e.target.value)} />
        </Field>
        <Field className="grow" label={t("supply.note")}>
          <input value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}
