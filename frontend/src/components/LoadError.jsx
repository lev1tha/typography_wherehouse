import { useTranslation } from "react-i18next";

import Icon from "./Icon.jsx";

// «Не удалось загрузить» с кнопкой «Повторить». Раньше ошибка загрузки
// превращалась в пустой список («Нет данных», «Заказов 0, долг 0»): человек
// принимал сбой сервера за состояние дел. Пустое и сломанное теперь выглядят
// по-разному.
export default function LoadError({ onRetry, message }) {
  const { t } = useTranslation();
  return (
    <div className="empty-state" role="alert">
      <Icon name="alert" size={40} className="es-icon" />
      <div>{message || t("common.loadFailed")}</div>
      {onRetry && (
        <div className="empty-action">
          <button type="button" onClick={onRetry}>
            {t("common.retry")}
          </button>
        </div>
      )}
    </div>
  );
}
