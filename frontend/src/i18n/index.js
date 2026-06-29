import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";
import ky from "./locales/ky.json";
import ru from "./locales/ru.json";

const saved = localStorage.getItem("lang") || "ru";

i18n.use(initReactI18next).init({
  resources: {
    ru: { translation: ru },
    ky: { translation: ky },
    en: { translation: en },
  },
  lng: saved,
  fallbackLng: "ru",
  interpolation: { escapeValue: false },
});

export default i18n;
