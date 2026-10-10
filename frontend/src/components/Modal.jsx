import { useTranslation } from "react-i18next";

import { useDialog } from "../hooks/useDialog.js";
import Icon from "./Icon.jsx";

// `wide` — для содержимого, которому 520px мало: таблица массового ввода
// каталога иначе показывала бы три колонки из одиннадцати.
//
// Окно закрывается крестиком, «Отменой» (если она есть в футере) и клавишей Esc;
// щелчок по затемнению НЕ закрывает. Формы здесь длинные — размеры, цены, ставка
// реза, — и промах мимо окна стирал всё введённое без предупреждения. Esc при
// уже введённых данных сперва спрашивает. Роль диалога, ловушка Tab, возврат
// фокуса и блокировка прокрутки фона — в `useDialog`: правка в одном месте
// закрывает все окна приложения, включая подтверждения.
export default function Modal({ title, onClose, children, footer, wide = false }) {
  const { t } = useTranslation();
  const { dialogProps, titleId, asking, keepEditing, discard } = useDialog({ onClose });

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        // Щелчок по затемнению не закрывает окно и не уводит фокус на страницу.
        if (e.target === e.currentTarget) {
          e.preventDefault();
          dialogProps.ref.current?.focus();
        }
      }}
    >
      <div className={wide ? "modal wide" : "modal"} {...dialogProps}>
        <div className="modal-head">
          <h2 id={titleId}>{title}</h2>
          <button className="ghost" onClick={onClose} aria-label={t("common.close")}>
            <Icon name="x" size={18} />
          </button>
        </div>
        {asking && (
          <div className="callout" role="alert">
            <p style={{ margin: "0 0 8px" }}>{t("modal.discardAsk")}</p>
            <div className="row" style={{ gap: 8 }}>
              <button type="button" autoFocus onClick={keepEditing}>{t("modal.keepEditing")}</button>
              <button type="button" className="secondary" onClick={discard}>{t("modal.discard")}</button>
            </div>
          </div>
        )}
        {children}
        {footer && <div className="row modal-footer" style={{ marginTop: 16 }}>{footer}</div>}
      </div>
    </div>
  );
}
