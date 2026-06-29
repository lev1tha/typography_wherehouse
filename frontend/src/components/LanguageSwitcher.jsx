import { useTranslation } from "react-i18next";

const LANGS = [
  { code: "ru", label: "Русский" },
  { code: "ky", label: "Кыргызча" },
  { code: "en", label: "English" },
];

export default function LanguageSwitcher() {
  const { i18n } = useTranslation();

  function change(code) {
    i18n.changeLanguage(code);
    localStorage.setItem("lang", code);
  }

  return (
    <select
      className="lang-select"
      value={i18n.resolvedLanguage}
      onChange={(e) => change(e.target.value)}
      aria-label="Language"
    >
      {LANGS.map((l) => (
        <option key={l.code} value={l.code}>
          {l.label}
        </option>
      ))}
    </select>
  );
}
