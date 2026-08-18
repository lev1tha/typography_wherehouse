import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Icon from "./Icon.jsx";

// Доступ к «Финансам» и «Подробной аналитике» открывается отдельным паролем и
// держится открытым 30 минут.
//
// Признак снятия — ПОДПИСАННЫЙ сервером токен, а не отметка времени. Раньше
// здесь лежал `financeUnlockedAt` с числом, и строки
// `localStorage.setItem('financeUnlockedAt', Date.now())` в консоли хватало,
// чтобы открыть раздел, не зная пароля. Подпись подделать нельзя, а срок
// проверяет сервер: браузеру тут верить не за что.
export const UNLOCK_KEY = "financeUnlockToken";

export default function FinanceGate({ children }) {
  const { t } = useTranslation();
  // null — ещё спрашиваем сервер; true/false — ответ получен.
  const [unlocked, setUnlocked] = useState(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const token = localStorage.getItem(UNLOCK_KEY);
    if (!token) return setUnlocked(false);
    api
      .get("/finance/unlock/", { headers: { "X-Finance-Unlock": token } })
      .then((r) => {
        if (r.data?.ok) return setUnlocked(true);
        localStorage.removeItem(UNLOCK_KEY);
        setUnlocked(false);
      })
      .catch(() => {
        localStorage.removeItem(UNLOCK_KEY);
        setUnlocked(false);
      });
  }, []);

  async function onSubmit(e) {
    e.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post("/finance/unlock/", { password });
      localStorage.setItem(UNLOCK_KEY, data.token);
      setPassword("");
      setUnlocked(true);
    } catch {
      setError(t("financeGate.error"));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  if (unlocked === null) return <p className="muted">{t("common.loading")}</p>;
  if (unlocked) return children;

  return (
    <div style={{ maxWidth: 380, margin: "8vh auto 0" }}>
      <div className="card" style={{ textAlign: "center" }}>
        <span
          style={{
            width: 56,
            height: 56,
            borderRadius: 16,
            margin: "0 auto 12px",
            background: "var(--primary-soft)",
            color: "var(--accent-strong)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Icon name="lock" size={28} />
        </span>
        <h2 style={{ margin: "0 0 4px" }}>{t("financeGate.title")}</h2>
        <p className="muted" style={{ marginTop: 0 }}>{t("financeGate.prompt")}</p>
        <form onSubmit={onSubmit} style={{ textAlign: "left" }}>
          <div className="field">
            <label>{t("common.password")}</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
              autoComplete="off"
              placeholder={t("financeGate.placeholder")}
            />
          </div>
          {error && <div className="error">{error}</div>}
          <button type="submit" style={{ width: "100%" }} disabled={busy}>
            {busy ? t("common.loading") : t("financeGate.submit")}
          </button>
        </form>
      </div>
    </div>
  );
}
