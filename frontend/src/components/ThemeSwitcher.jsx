import { useState } from "react";
import { useTranslation } from "react-i18next";

import { applyTheme, readTheme } from "../theme.js";
import Icon from "./Icon.jsx";

// Переключатель темы в шапке: система · светлая · тёмная. Три кнопки, а не
// галочка — «как в системе» это отдельный осмысленный выбор, а не отсутствие
// выбора: у половины телефонов тёмная включается по расписанию сама.
const OPTIONS = [
  { key: "system", icon: "monitor" },
  { key: "light", icon: "sun" },
  { key: "dark", icon: "moon" },
];

export default function ThemeSwitcher() {
  const { t } = useTranslation();
  const [theme, setTheme] = useState(readTheme);

  function pick(next) {
    applyTheme(next);
    setTheme(next);
  }

  return (
    <div className="lang" role="group" aria-label={t("theme.label")}>
      {OPTIONS.map((o) => (
        <button
          key={o.key}
          className={theme === o.key ? "active" : ""}
          onClick={() => pick(o.key)}
          title={t(`theme.${o.key}`)}
          aria-label={t(`theme.${o.key}`)}
          aria-pressed={theme === o.key}
          style={{ padding: "0 8px" }}
        >
          <Icon name={o.icon} size={16} />
        </button>
      ))}
    </div>
  );
}
