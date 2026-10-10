import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import { isCanceled, useLatest } from "../utils/latest.js";

// Выбор клиента с поиском на сервере.
//
// Раньше «клиент», «кто привёл» и «склеить с…» были обычными <select>, набитыми
// из `/clients/clients/` БЕЗ page_size, то есть первыми 25 клиентами из базы. С
// сотней клиентов нужного в списке просто не было, а в карточке клиента при
// поиске у реферера оставалась единственная опция «— никто —»: администратор,
// не заметив этого, мог выбрать её и затереть настоящего реферера.
//
// Здесь список подгружается по набранному тексту (?search=, 20 штук, с паузой в
// 250 мс и отменой устаревших запросов). Выбранный клиент показывается всегда —
// даже если его нет в текущей выдаче: подпись берётся из `valueLabel`, а если её
// нет — подгружается по id.
//
// Props:
//   value           id выбранного клиента (число/строка) или "" / null
//   onChange(id, c) id — число или "" (сняли выбор); c — объект клиента или null
//   valueLabel      подпись выбранного, если уже известна (экономит запрос)
//   excludeIds      кого не предлагать (сам клиент, клиенты, дающие кольцо)
//   noneLabel       текст пункта «никого / все»; без него снять выбор нельзя
//   placeholder     подсказка в пустом поле
const labelOf = (c) => `${c.display_name}${c.phone ? ` (${c.phone})` : ""}`;
const labels = new Map(); // id → подпись, живёт до перезагрузки страницы

export default function ClientPicker({
  value,
  onChange,
  valueLabel,
  excludeIds = [],
  noneLabel,
  placeholder,
  disabled = false,
  id: idProp,
  className = "",
  style,
  ...aria
}) {
  const { t } = useTranslation();
  const uid = useId().replace(/:/g, "");
  const inputId = idProp || `cp${uid}`;
  const listId = `${inputId}-list`;
  const next = useLatest();
  const wrap = useRef(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [active, setActive] = useState(0);
  const [fetched, setFetched] = useState("");

  const hasValue = value !== "" && value != null;
  if (hasValue && valueLabel) labels.set(String(value), valueLabel);
  const selectedLabel = hasValue ? valueLabel || labels.get(String(value)) || fetched : "";

  // Выбранного нет в выдаче и подписи нет — спрашиваем сервер по id.
  useEffect(() => {
    if (!hasValue || valueLabel || labels.has(String(value))) return undefined;
    let alive = true;
    api
      .get(`/clients/clients/${value}/`)
      .then((r) => {
        const l = labelOf(r.data);
        labels.set(String(value), l);
        if (alive) setFetched(l);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [value, valueLabel, hasValue]);

  // Поиск: открыто — грузим по набранному тексту.
  useEffect(() => {
    if (!open) return undefined;
    const handle = setTimeout(() => {
      setLoading(true);
      setFailed(false);
      const pageSize = 20 + excludeIds.length;
      api
        .get("/clients/clients/", {
          params: { ...(query.trim() ? { search: query.trim() } : {}), page_size: pageSize },
          signal: next(),
        })
        .then((r) => {
          const rows = (r.data.results || []).filter((c) => !excludeIds.includes(c.id));
          setItems(rows.slice(0, 20));
          setTotal(Number(r.data.count) || rows.length);
          setActive(0);
          setLoading(false);
        })
        .catch((e) => {
          if (isCanceled(e)) return;
          setFailed(true);
          setLoading(false);
        });
    }, query ? 250 : 0);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, query, excludeIds.join(",")]);

  // Список пунктов: «никого» (если задан) + найденные клиенты.
  const options = [
    ...(noneLabel ? [{ none: true, key: "none" }] : []),
    ...items.map((c) => ({ c, key: String(c.id) })),
  ];

  function choose(opt) {
    setOpen(false);
    setQuery("");
    if (opt.none) onChange("", null);
    else {
      labels.set(String(opt.c.id), labelOf(opt.c));
      onChange(opt.c.id, opt.c);
    }
  }

  function onKeyDown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!open) return setOpen(true);
      setActive((a) => Math.min(a + 1, Math.max(0, options.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter" && open) {
      // Enter внутри окна с формой не должен «сохранять»: выбираем пункт.
      e.preventDefault();
      const opt = options[active];
      if (opt) choose(opt);
    } else if (e.key === "Escape" && open) {
      // Закрываем только список; окно под ним остаётся.
      e.stopPropagation();
      setOpen(false);
      setQuery("");
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  const shown = open ? query : selectedLabel;
  const activeId = open && options[active] ? `${inputId}-o${options[active].key}` : undefined;

  return (
    <div className={`picker ${className}`} ref={wrap} style={style}>
      <input
        {...aria}
        id={inputId}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeId}
        autoComplete="off"
        disabled={disabled}
        value={shown}
        placeholder={open ? t("picker.search") : placeholder || (noneLabel ? `— ${noneLabel} —` : t("picker.search"))}
        onFocus={() => setOpen(true)}
        onClick={() => setOpen(true)}
        onBlur={() => {
          setOpen(false);
          setQuery("");
        }}
        onChange={(e) => {
          setOpen(true);
          setQuery(e.target.value);
        }}
        onKeyDown={onKeyDown}
      />
      {open && (
        <ul className="picker-list" id={listId} role="listbox">
          {options.map((o, i) => (
            <li
              key={o.key}
              id={`${inputId}-o${o.key}`}
              role="option"
              aria-selected={o.none ? !hasValue : String(o.c.id) === String(value)}
              className={`picker-opt${i === active ? " active" : ""}${o.none ? " none" : ""}`}
              // mousedown, а не click: к click поле уже потеряло бы фокус и
              // закрыло список раньше, чем выбор состоялся.
              onMouseDown={(e) => {
                e.preventDefault();
                choose(o);
              }}
              onMouseEnter={() => setActive(i)}
            >
              {o.none ? (
                <span className="muted">— {noneLabel} —</span>
              ) : (
                <>
                  <span className="picker-name">{o.c.display_name}</span>
                  {o.c.phone && <span className="muted picker-phone">{o.c.phone}</span>}
                </>
              )}
            </li>
          ))}
          {loading && <li className="picker-note" role="presentation">{t("common.loading")}</li>}
          {failed && <li className="picker-note" role="presentation">{t("common.error")}</li>}
          {!loading && !failed && !items.length && (
            <li className="picker-note" role="presentation">{t("picker.nothing")}</li>
          )}
          {!loading && total > items.length && items.length > 0 && (
            <li className="picker-note" role="presentation">
              {t("picker.more", { shown: items.length, total })}
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
