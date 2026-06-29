import { createContext, useContext, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import Modal from "./Modal.jsx";

const UIContext = createContext(null);

let _id = 0;

export function UIProvider({ children }) {
  const { t } = useTranslation();
  const [toasts, setToasts] = useState([]);
  const [confirmState, setConfirmState] = useState(null); // { message, resolve }
  const timers = useRef({});

  function toast(message, type = "success") {
    const id = ++_id;
    setToasts((prev) => [...prev, { id, message, type }]);
    timers.current[id] = setTimeout(() => {
      setToasts((prev) => prev.filter((x) => x.id !== id));
    }, 3200);
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
        {toasts.map((x) => (
          <div key={x.id} className={`toast ${x.type}`}>
            {x.message}
          </div>
        ))}
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

export function useUI() {
  const ctx = useContext(UIContext);
  if (!ctx) throw new Error("useUI must be used within UIProvider");
  return ctx;
}
