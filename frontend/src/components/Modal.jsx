import { useTranslation } from "react-i18next";

import Icon from "./Icon.jsx";

// `wide` — для содержимого, которому 520px мало: таблица массового ввода
// каталога иначе показывала бы три колонки из одиннадцати.
//
// Окно закрывается ТОЛЬКО крестиком (и «Отменой», если она есть в футере):
// щелчок по затемнению больше не закрывает. Формы здесь длинные — размеры,
// цены, ставка реза, — и промах мимо окна стирал всё введённое без
// предупреждения. Закрытие должно быть намеренным.
export default function Modal({ title, onClose, children, footer, wide = false }) {
  const { t } = useTranslation();
  return (
    <div className="modal-backdrop">
      <div className={wide ? "modal wide" : "modal"}>
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
