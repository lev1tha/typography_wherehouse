import { useCallback, useEffect, useId, useRef, useState } from "react";

// Поведение окна-диалога — в одном месте, для всех окон приложения.
//
// Что обеспечивает:
//   * role="dialog" + aria-modal + aria-labelledby (имя окна для диктора);
//   * фокус внутрь при открытии: первое поле формы, иначе первая кнопка футера
//     (в окне подтверждения это «Отмена», а не «Подтвердить» — случайный Enter
//     не должен удалять), иначе крестик;
//   * Tab/Shift+Tab ходят по кругу внутри окна, фокус не убегает на страницу
//     под ним; для вложенных окон реагирует только верхнее;
//   * Esc закрывает окно. Если в окне уже что-то вводили — сперва спрашивает
//     (щелчок по затемнению окно не закрывает по той же причине: длинные формы
//     нельзя терять одним промахом);
//   * после закрытия фокус возвращается на элемент, который открыл окно;
//   * пока открыто хоть одно окно, страница под ним не прокручивается.
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

const stack = []; // открытые окна; верхнее — последнее
let locks = 0;
let savedOverflow = "";
let savedPadding = "";

function lockScroll() {
  if (locks++ === 0) {
    const body = document.body;
    savedOverflow = body.style.overflow;
    savedPadding = body.style.paddingRight;
    // Полоса прокрутки исчезает вместе с overflow:hidden — без компенсации вся
    // страница под окном дёргается вправо на её ширину.
    const bar = window.innerWidth - document.documentElement.clientWidth;
    if (bar > 0) body.style.paddingRight = `${bar}px`;
    body.style.overflow = "hidden";
  }
}

function unlockScroll() {
  if (--locks <= 0) {
    locks = 0;
    document.body.style.overflow = savedOverflow;
    document.body.style.paddingRight = savedPadding;
  }
}

const isVisible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
const focusables = (root) => [...root.querySelectorAll(FOCUSABLE)].filter(isVisible);

// Ввод в поле поиска/выбора — не «несохранённые данные».
const isTransient = (el) =>
  el?.matches?.('[role="combobox"], input[type="search"], input[type="date"][data-transient]');

export function useDialog({ onClose, guardInput = true }) {
  const ref = useRef(null);
  const raw = useId();
  const titleId = `dlg${raw.replace(/:/g, "")}`;
  const dirty = useRef(false);
  const [asking, setAskingState] = useState(false);
  // Зеркало состояния для обработчика клавиш: побочные эффекты (закрытие окна)
  // нельзя делать внутри функции-обновления setState — StrictMode вызовет её дважды.
  const askingRef = useRef(false);
  const beforeAsk = useRef(null); // что было в фокусе, пока не появился вопрос
  const setAsking = useCallback((v) => {
    if (v) beforeAsk.current = document.activeElement;
    askingRef.current = v;
    setAskingState(v);
    if (!v) {
      // Вопрос исчез вместе с кнопкой, на которой стоял фокус, — без явного
      // возврата фокус уходил на <body>, и следующий Esc окно уже не слышало.
      const back = beforeAsk.current;
      requestAnimationFrame(() => {
        const el = ref.current;
        if (back && el && el.contains(back) && document.contains(back)) back.focus({ preventScroll: true });
        else el?.focus({ preventScroll: true });
      });
    }
  }, []);
  // Элемент, открывший окно. Запоминаем при ПЕРВОЙ отрисовке: к моменту
  // эффекта поле с autoFocus уже забрало фокус себе, и «вернуть фокус на
  // открывший» возвращало бы его на поле, которого больше нет.
  const restoreTimer = useRef(null);
  const openerRef = useRef(undefined);
  if (openerRef.current === undefined && typeof document !== "undefined") {
    openerRef.current = document.activeElement;
  }
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const opener = openerRef.current;
    // StrictMode монтирует эффект дважды: между прогонами cleanup возвращал бы
    // фокус на открывшую кнопку, и повторный прогон не находил бы поле с
    // autoFocus. Возврат фокуса отложен на такт и отменяется повторным запуском.
    clearTimeout(restoreTimer.current);
    stack.push(el);
    lockScroll();

    // Начальный фокус. Если поле уже забрало его само (autoFocus) — не мешаем.
    if (!el.contains(document.activeElement)) {
      const body = [...el.querySelectorAll("input:not([type='hidden']):not([disabled]), select:not([disabled]), textarea:not([disabled])")].filter(
        (n) => isVisible(n) && !n.closest(".modal-head")
      );
      const footerBtn = el.querySelector(":scope > .modal-footer button:not([disabled])");
      const target = body[0] || footerBtn || el.querySelector(".modal-head button") || el;
      target.focus({ preventScroll: true });
    }

    // Фокус, ушедший из верхнего окна (щелчок по затемнению), возвращаем.
    const onFocusIn = (e) => {
      if (stack[stack.length - 1] !== el) return;
      if (el.contains(e.target) || e.target.closest?.(".toasts")) return;
      const nodes = focusables(el);
      (nodes[0] || el).focus({ preventScroll: true });
    };
    document.addEventListener("focusin", onFocusIn);

    return () => {
      document.removeEventListener("focusin", onFocusIn);
      const i = stack.indexOf(el);
      if (i >= 0) stack.splice(i, 1);
      unlockScroll();
      restoreTimer.current = setTimeout(() => {
        if (opener && opener !== document.body && document.contains(opener) && typeof opener.focus === "function") {
          opener.focus({ preventScroll: true });
        }
      }, 0);
    };
  }, []);

  const onKeyDown = useCallback((e) => {
    const el = ref.current;
    if (!el) return;
    if (e.key === "Escape" && !e.defaultPrevented) {
      e.stopPropagation();
      e.preventDefault();
      if (askingRef.current) setAsking(false); // второй Esc — «продолжить правку»
      else if (dirty.current) setAsking(true);
      else closeRef.current?.();
      return;
    }
    if (e.key !== "Tab") return;
    const nodes = focusables(el);
    if (!nodes.length) {
      e.preventDefault();
      el.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === el)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }, [setAsking]);

  const onInput = useCallback((e) => {
    if (guardInput && !isTransient(e.target)) dirty.current = true;
  }, [guardInput]);

  return {
    asking,
    keepEditing: () => setAsking(false),
    discard: () => closeRef.current?.(),
    dialogProps: {
      ref,
      role: "dialog",
      "aria-modal": true,
      "aria-labelledby": titleId,
      tabIndex: -1,
      onKeyDown,
      onInput,
    },
    titleId,
  };
}
