import { createContext, useContext, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import Icon from "./Icon.jsx";
import Modal from "./Modal.jsx";

const UIContext = createContext(null);

let _id = 0;

export function UIProvider({ children }) {
  const { t } = useTranslation();
  const [toasts, setToasts] = useState([]);
  const [confirmState, setConfirmState] = useState(null); // { message, resolve }
  const timers = useRef({});

  // Зеркало списка: решение «такое уже показано?» принимается вне функции-
  // обновления состояния (StrictMode вызывает её дважды, а таймер — побочный
  // эффект).
  const live = useRef([]);
  const publish = (next) => {
    live.current = next;
    setToasts(next);
  };

  function dismiss(id) {
    clearTimeout(timers.current[id]);
    delete timers.current[id];
    publish(live.current.filter((x) => x.id !== id));
  }

  // Типы: "success" (гаснет сама через 3,2 с), "error" и "warning" (остаются, пока
  // их не закроют крестиком). Ошибку, которая исчезла за 3 секунды, можно
  // просто не успеть прочитать — а по ней как раз решают, что делать дальше.
  function toast(message, type = "success") {
    const text = typeof message === "string" ? message : String(message ?? "");
    // Одинаковое сообщение подряд не плодим: пять нажатий на сломанную кнопку —
    // это одна ошибка на экране, а не стопка из пяти.
    const same = live.current.find((x) => x.type === type && x.message === text);
    if (same) {
      if (type === "success") {
        clearTimeout(timers.current[same.id]);
        timers.current[same.id] = setTimeout(() => dismiss(same.id), 3200);
      }
      return;
    }
    const id = ++_id;
    if (type === "success") timers.current[id] = setTimeout(() => dismiss(id), 3200);
    publish([...live.current, { id, message: text, type }].slice(-5));
  }

  function confirm(message) {
    return new Promise((resolve) => setConfirmState({ message, resolve }));
  }

  function closeConfirm(result) {
    confirmState?.resolve(result);
    setConfirmState(null);
  }

  return (
    <UIContext.Provider value={{ toast, confirm }}>
      {children}

      <div className="toasts">
        {/* Две области: обычные сообщения озвучиваются вежливо, ошибки и
            предупреждения — сразу. Обе всегда в разметке, иначе диктор не
            услышит то, что появилось вместе с самой областью. */}
        <div className="toast-region" role="status" aria-live="polite">
          {toasts
            .filter((x) => x.type === "success")
            .map((x) => (
              <Toast key={x.id} toast={x} onClose={dismiss} />
            ))}
        </div>
        <div className="toast-region" role="alert" aria-live="assertive">
          {toasts
            .filter((x) => x.type !== "success")
            .map((x) => (
              <Toast key={x.id} toast={x} onClose={dismiss} />
            ))}
        </div>
      </div>

      {confirmState && (
        <Modal
          title={t("common.confirm")}
          onClose={() => closeConfirm(false)}
          footer={
            <>
              <button className="secondary" onClick={() => closeConfirm(false)}>
                {t("common.cancel")}
              </button>
              <button className="danger" onClick={() => closeConfirm(true)}>
                {t("common.confirm")}
              </button>
            </>
          }
        >
          <p>{confirmState.message}</p>
        </Modal>
      )}
    </UIContext.Provider>
  );
}

function Toast({ toast, onClose }) {
  const { t } = useTranslation();
  return (
    <div className={`toast ${toast.type}`}>
      <span className="toast-text">{toast.message}</span>
      <button
        type="button"
        className="toast-close"
        onClick={() => onClose(toast.id)}
        aria-label={t("common.close")}
      >
        <Icon name="x" size={16} />
      </button>
    </div>
  );
}

export function useUI() {
  const ctx = useContext(UIContext);
  if (!ctx) throw new Error("useUI must be used within UIProvider");
  return ctx;
}
