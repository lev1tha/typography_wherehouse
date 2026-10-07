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
  server: {
    port: Number(process.env.FRONTEND_PORT || process.env.PORT) || 5710,
    // Proxy API calls to Django in dev so the frontend can use relative /api.
    proxy: {
      "/api": API,
      "/media": API,
    },
  },
});
