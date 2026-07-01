import { useState } from "react";
import { useTranslation } from "react-i18next";

import api from "../api/api.js";
import Icon from "./Icon.jsx";

// Доступ к «Финансам» и «Подробной аналитике» открывается отдельным паролем
// и держится открытым 30 минут (метка времени в localStorage).
const UNLOCK_KEY = "financeUnlockedAt";
const WINDOW_MS = 30 * 60 * 1000;

function unlockedRecently() {
  const ts = Number(localStorage.getItem(UNLOCK_KEY) || 0);
  return ts > 0 && Date.now() - ts < WINDOW_MS;
}

export default function FinanceGate({ children }) {
  const { t } = useTranslation();
  const [unlocked, setUnlocked] = useState(unlockedRecently);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (unlocked) return children;

  async function onSubmit(e) {
    e.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError("");
    try {
      await api.post("/finance/unlock/", { password });
      localStorage.setItem(UNLOCK_KEY, String(Date.now()));
      setPassword("");
      setUnlocked(true);
    } catch {
      setError(t("financeGate.error"));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

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
