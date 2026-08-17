/**
 * Выпадашка справочника, в которой можно завести новое значение не уходя.
 *
 * Тип материала и производство — справочники, а не список в коде: сервер
 * принимает создание по одному названию (код генерирует сам). Но экрана для
 * этого не было, и добавить третье производство можно было только через
 * Django-админку — про которую заказчик знать не обязан. В Финансах виды
 * расхода давно заводятся кнопкой прямо в отчёте; здесь теперь так же.
 *
 * Последним пунктом списка идёт «+ добавить…»: выбрал — ячейка превратилась в
 * поле, ввёл название, Enter — значение создано и сразу выбрано. Esc отменяет.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { apiError } from "../api/errors.js";
import { useUI } from "./UIProvider.jsx";

const ADD = "__add__";

export default function RefSelect({
  value,
  options = [],
  onChange,
  endpoint,
  onCreated,
  ...rest
}) {
  const { t } = useTranslation();
  const { toast } = useUI();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const current = value == null ? "" : String(value);
  // Значение, которого нет в справочнике (вставили название из буфера) —
  // показываем как есть, иначе ячейка выглядит пустой и человек думает, что
  // вставка не сработала.
  const unknown = current && !options.some((o) => String(o.id) === current);

  async function save() {
    const clean = name.trim();
    if (!clean) {
      setAdding(false);
      return;
    }
    // Такое название уже есть — просто выбираем его, ничего не создавая.
    // Раньше сервер отвечал «уже существует», текст оставался в поле, и
    // документ уезжал без поставщика — человек-то видел имя в поле.
    const existing = options.find((o) => String(o.name).trim().toLowerCase() === clean.toLowerCase());
    if (existing) {
      onChange(String(existing.id));
      setAdding(false);
      setName("");
      return;
    }
    setBusy(true);
    try {
      const { data } = await api.post(endpoint, { name: clean });
      await onCreated?.(data);
      onChange(String(data.id));
      setAdding(false);
      setName("");
    } catch (e) {
      toast(apiError(e, t("common.error")), "error");
      // Не создалось — возвращаем список: пустая ячейка честнее имени, которого нет.
      setAdding(false);
      setName("");
    } finally {
      setBusy(false);
    }
  }

  if (adding) {
    return (
      <input
        {...rest}
        autoFocus
        disabled={busy}
        value={name}
        placeholder={t("warehouse.newRefPh")}
        onChange={(e) => setName(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            save();
          } else if (e.key === "Escape") {
            setName("");
            setAdding(false);
          }
        }}
      />
    );
  }

  return (
    <select
      {...rest}
      value={current}
      onChange={(e) => (e.target.value === ADD ? setAdding(true) : onChange(e.target.value))}
    >
      <option value="">—</option>
      {options.map((option) => (
        <option key={option.id} value={option.id}>{option.name}</option>
      ))}
      {unknown && <option value={current}>{current} — ?</option>}
      <option value={ADD}>{t("warehouse.addRef")}</option>
    </select>
  );
}
