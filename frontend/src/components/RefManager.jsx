/**
 * Правка справочника: переименовать, скрыть, удалить неиспользуемое.
 *
 * Завести новое значение `RefSelect` умел с самого начала, а вот исправить —
 * нет. Опечатка в названии типа («Оргстекл») жила вечно: сервер правку
 * принимает (`PATCH`), экрана для неё не было, и оставалась Django-админка —
 * про которую заказчик знать не обязан.
 *
 * Три действия и разные последствия, поэтому они разведены:
 *   переименовать — можно всегда, включая встроенные (меняется только подпись);
 *   скрыть        — убирает из списков, заведённые материалы не трогает;
 *   удалить       — только то, на чём НИЧЕГО не висит и что не встроено.
 *
 * Удаление занятого значения сервер и так превращает в скрытие (FK на PROTECT),
 * но кнопку «Удалить» на нём не показываем вовсе: нажать и получить не то, что
 * написано на кнопке, — хуже, чем не иметь кнопки.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";
import { useUI } from "./UIProvider.jsx";

export default function RefManager({ title, endpoint, options = [], onDone, onClose }) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [busyId, setBusyId] = useState(null);
  // Черновики имён: правим по месту, сохраняем по уходу из поля.
  const [draft, setDraft] = useState({});

  async function run(id, fn) {
    setBusyId(id);
    try {
      await fn();
      await onDone?.();
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
    } finally {
      setBusyId(null);
    }
  }

  const rename = (row, name) => {
    const clean = name.trim();
    if (!clean || clean === row.name) return;
    run(row.id, () => api.patch(`${endpoint}${row.id}/`, { name: clean }));
  };

  const hide = (row) =>
    run(row.id, () => api.patch(`${endpoint}${row.id}/`, { is_archived: true }));

  const show = (row) =>
    run(row.id, () => api.patch(`${endpoint}${row.id}/`, { is_archived: false }));

  const remove = (row) => run(row.id, () => api.delete(`${endpoint}${row.id}/`));

  return (
    <Modal title={title} onClose={onClose} footer={
      <button className="secondary" onClick={onClose}>{t("common.close")}</button>
    }>
      {options.length === 0 && <p className="muted">{t("warehouse.refEmpty")}</p>}
      {options.map((row) => {
        const used = Number(row.materials_count) || 0;
        const canDelete = used === 0 && !row.is_builtin;
        return (
          <div key={row.id} className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8 }}>
            <div className="field grow" style={{ margin: 0 }}>
              <input
                defaultValue={row.name}
                disabled={busyId === row.id}
                onChange={(e) => setDraft((s) => ({ ...s, [row.id]: e.target.value }))}
                onBlur={() => rename(row, draft[row.id] ?? row.name)}
                // Enter сохраняет САМ, а не через `blur()`: попытка сохранить
                // уходом из поля молча не срабатывала, и правка терялась —
                // выглядело так, будто переименование не работает вовсе.
                onKeyDown={(e) => {
                  if (e.key !== "Enter") return;
                  e.preventDefault();
                  rename(row, e.target.value);
                }}
              />
            </div>
            {/* Сколько материалов на нём висит — чтобы было видно, почему
                удалить нельзя, ещё до нажатия. */}
            <span className="muted" style={{ fontSize: 12, whiteSpace: "nowrap", minWidth: 78 }}>
              {used > 0 ? t("warehouse.refUsedBy", { n: used }) : t("warehouse.refUnused")}
            </span>
            {row.is_archived ? (
              <button className="secondary" disabled={busyId === row.id} onClick={() => show(row)}>
                {t("warehouse.refShow")}
              </button>
            ) : (
              <button className="secondary" disabled={busyId === row.id} onClick={() => hide(row)}>
                {t("warehouse.refHide")}
              </button>
            )}
            {canDelete && (
              <button
                className="ghost"
                disabled={busyId === row.id}
                onClick={() => remove(row)}
                title={t("common.delete")}
                aria-label={t("common.delete")}
              >
                <Icon name="trash" size={16} />
              </button>
            )}
          </div>
        );
      })}
      <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
        {t("warehouse.refManagerHint")}
      </p>
    </Modal>
  );
}
