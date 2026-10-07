import { useId, useRef, useState } from "react";

import Icon from "./Icon.jsx";

// Подсказка к термину: значок «ⓘ», по наведению, фокусу или нажатию — короткое
// объяснение простыми словами (2026-10-07). Владелец — не бухгалтер: «EBITDA»,
// «валовая прибыль», «чистый денежный поток» без пояснения читаются как шум.
//
// Всплывашка стоит `position: fixed` по координатам значка: внутри таблиц
// отчётов с горизонтальной прокруткой обычная `absolute` обрезалась бы
// краем таблицы. Нажатие держит её открытой — на телефоне наведения нет.
const WIDTH = 300;

export default function Hint({ text, tone = "info", label }) {
  const [pos, setPos] = useState(null);
  const [pinned, setPinned] = useState(false);
  const btn = useRef(null);
  const id = useId();
  if (!text) return null;

  function place() {
    const r = btn.current?.getBoundingClientRect();
    if (!r) return;
    const width = Math.min(WIDTH, window.innerWidth - 32);
    const left = Math.max(16, Math.min(r.left + r.width / 2 - width / 2, window.innerWidth - width - 16));
    const below = r.bottom + 6;
    // У нижнего края экрана — над значком.
    const top = below + 120 > window.innerHeight ? Math.max(8, r.top - 6 - 120) : below;
    setPos({ left, top, width });
  }
  const hide = () => {
    if (!pinned) setPos(null);
  };

  return (
    <span className={`hint hint-${tone}`} onMouseEnter={place} onMouseLeave={hide}>
      <button
        ref={btn}
        type="button"
        className="hint-btn"
        aria-label={label || text}
        aria-describedby={pos ? id : undefined}
        aria-expanded={!!pos}
        onFocus={place}
        onBlur={() => {
          setPinned(false);
          setPos(null);
        }}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          if (pinned) {
            setPinned(false);
            setPos(null);
          } else {
            setPinned(true);
            place();
          }
        }}
      >
        <Icon name={tone === "warn" ? "alert" : "info"} size={14} />
      </button>
      {pos && (
        <span
          role="tooltip"
          id={id}
          className="hint-pop"
          style={{ left: pos.left, top: pos.top, width: pos.width }}
        >
          {text}
        </span>
      )}
    </span>
  );
}
