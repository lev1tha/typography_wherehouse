import { useTranslation } from "react-i18next";

import Icon from "./Icon.jsx";

// `wide` — для содержимого, которому 520px мало: таблица массового ввода
// каталога иначе показывала бы три колонки из одиннадцати.
export default function Modal({ title, onClose, children, footer, wide = false }) {
  const { t } = useTranslation();
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className={wide ? "modal wide" : "modal"} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="ghost" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" size={18} />
          </button>
        </div>
        {children}
        {footer && <div className="row" style={{ marginTop: 16 }}>{footer}</div>}
      </div>
    </div>
  );
}
