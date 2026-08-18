import { createContext, useContext, useEffect, useState } from "react";

import api from "../api/api.js";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    const raw = localStorage.getItem("user");
    return raw ? JSON.parse(raw) : null;
  });
  const [loading, setLoading] = useState(false);

  async function login(username, password) {
    const { data } = await api.post("/token/", { username, password });
    localStorage.setItem("userToken", data.access);
    localStorage.setItem("refreshToken", data.refresh);
    localStorage.setItem("user", JSON.stringify(data.user));
    setUser(data.user);
    return data.user;
  }

  async function loginCustomer(phone, password) {
    const { data } = await api.post("/customer/login/", {
      phone,
      ...(password ? { password } : {}),
    });
    // Без токена сервер сообщает, что делать дальше: задать пароль (первый вход)
    // или ввести существующий. Вход завершается только когда пришёл access.
    if (!data.access) {
      return { loggedIn: false, status: data.status, name: data.name };
    }
    const u = {
      role: "CUSTOMER",
      name: data.client.name,
      clientId: data.client.id,
      phone: data.client.phone,
    };
    localStorage.setItem("userToken", data.access);
    localStorage.removeItem("refreshToken");
    localStorage.setItem("user", JSON.stringify(u));
    setUser(u);
    return { loggedIn: true, user: u };
  }

  function logout() {
    localStorage.removeItem("userToken");
    localStorage.removeItem("refreshToken");
    localStorage.removeItem("user");
    // Снятый финансовый пароль уходит вместе с сессией. Раньше он оставался, и
    // на общей кассовой машине следующий вошедший попадал в «Финансы» без
    // пароля — все оставшиеся полчаса.
    localStorage.removeItem("financeUnlockToken");
    setUser(null);
  }

  // Keep auth in sync across tabs.
  useEffect(() => {
    function onStorage(e) {
      if (e.key === "user") {
        setUser(e.newValue ? JSON.parse(e.newValue) : null);
      }
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const value = {
    user,
    loading,
    setLoading,
    login,
    loginCustomer,
    logout,
    isAuthenticated: !!user,
    isAdmin: user?.role === "ADMIN",
    isAccountant: user?.role === "ACCOUNTANT",
    isCustomer: user?.role === "CUSTOMER",
    // Кому показывать закупочные цифры — себестоимость и маржу. У складовщика
    // их нет, у бухгалтера они и есть работа. Отдельно от `isAdmin`, потому что
    // «видит деньги» и «может править» — разные вопросы.
    seesMoney: user?.role === "ADMIN" || user?.role === "ACCOUNTANT",
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
