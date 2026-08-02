import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Django dev server. Порт 8000 бывает занят чужим процессом — тогда бэкенд
// поднимают на другом, и фронт должен знать куда стучаться: API_TARGET=…
const API = process.env.API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.PORT) || 5173,
    // Proxy API calls to Django in dev so the frontend can use relative /api.
    proxy: {
      "/api": API,
      "/media": API,
    },
  },
});
