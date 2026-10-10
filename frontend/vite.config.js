import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Свои порты у этого проекта: на машине одновременно живут несколько
// проектов, и стандартные 8000/5173 почти всегда заняты чужими.
//   бэкенд — 8710 (`manage.py runserver` поднимается на нём сам),
//   фронт  — 5710 (занят — Vite возьмёт следующий свободный).
// Переопределить: BACKEND_PORT=… (его же читает manage.py — бэкенд и прокси
// двигаются вместе), FRONTEND_PORT=…, или API_TARGET=… целиком.
const BACKEND_PORT = process.env.BACKEND_PORT || "8710";
const API = process.env.API_TARGET || `http://127.0.0.1:${BACKEND_PORT}`;

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Библиотеки — отдельными файлами: они меняются редко и остаются в кэше
        // браузера между выкладками, пока правится только код экранов.
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (/node_modules\/(react|react-dom|scheduler|react-router|react-router-dom|@remix-run)\//.test(id)) return "vendor-react";
          if (/node_modules\/(i18next|react-i18next|html-parse-stringify|void-elements)\//.test(id)) return "vendor-i18n";
          if (/node_modules\/(axios|form-data|follow-redirects|proxy-from-env|asynckit|combined-stream|mime-types|mime-db|delayed-stream)\//.test(id)) return "vendor-http";
          return undefined;
        },
      },
    },
  },
  server: {
    port: Number(process.env.FRONTEND_PORT || process.env.PORT) || 5710,
    // Proxy API calls to Django in dev so the frontend can use relative /api.
    proxy: {
      "/api": API,
      "/media": API,
    },
  },
});
