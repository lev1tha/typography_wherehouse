import axios from "axios";

// In dev, Vite proxies /api to Django. In prod set VITE_API_URL.
const BASE_URL = import.meta.env.VITE_API_URL || "/api";

// 30 секунд — предел ожидания. Без таймаута запрос на «Оформить» при обрыве сети
// висел бесконечно, а кассир не знал, оформился чек или нет. Теперь по таймауту
// приходит понятная ошибка (см. errors.js), а повтор уходит с тем же ключом
// идемпотентности (utils/idempotency.js) и второго чека не создаёт.
const api = axios.create({
  baseURL: BASE_URL,
  headers: { "Content-Type": "application/json" },
  timeout: 30000,
});

// Attach the JWT to every request.
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem("userToken");
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    const lang = localStorage.getItem("lang");
    if (lang) {
      config.headers["Accept-Language"] = lang;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Запросы входа и обновления токена: 401 на них означает «неверный пароль» или
// «сессия умерла окончательно», а не «токен просрочился» — их не перезапускаем.
const AUTH_URLS = ["/token/", "/token/refresh/", "/customer/login/"];
const isAuthUrl = (url = "") => AUTH_URLS.some((u) => url === u || url.endsWith(u));

function endSession() {
  localStorage.removeItem("userToken");
  localStorage.removeItem("refreshToken");
  localStorage.removeItem("user");
  localStorage.removeItem("financeUnlockToken");
  if (window.location.pathname !== "/login") {
    window.location.href = "/login";
  }
}

// Один общий refresh на все одновременные 401: если на экране в момент
// истечения токена летят пять запросов, обновление уходит один раз, а не пять.
// Простой axios (не `api`), чтобы запрос обновления не попал в этот же
// перехватчик и не зациклился.
let refreshing = null;
function refreshAccess() {
  if (!refreshing) {
    const refresh = localStorage.getItem("refreshToken");
    if (!refresh) return Promise.reject(new Error("no refresh token"));
    refreshing = axios
      .post(`${BASE_URL}/token/refresh/`, { refresh }, { timeout: 15000 })
      .then((r) => {
        localStorage.setItem("userToken", r.data.access);
        // Если сервер вращает refresh-токены — запоминаем новый.
        if (r.data.refresh) localStorage.setItem("refreshToken", r.data.refresh);
        return r.data.access;
      })
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

// 401: тихо обновляем токен и повторяем исходный запрос; не вышло — выход.
// Раньше через 12 часов (срок access) любой запрос уводил на /login и терял
// всё несохранённое; refresh-токен лежал в localStorage, но не использовался.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const { response, config } = error;
    if (response?.status !== 401 || !config) return Promise.reject(error);
    if (isAuthUrl(config.url)) return Promise.reject(error);

    if (!config._retried && localStorage.getItem("refreshToken")) {
      config._retried = true;
      try {
        const access = await refreshAccess();
        config.headers = { ...config.headers, Authorization: `Bearer ${access}` };
        return api(config);
      } catch {
        // refresh недействителен/истёк — обычный выход ниже
      }
    }
    endSession();
    return Promise.reject(error);
  }
);

export default api;
