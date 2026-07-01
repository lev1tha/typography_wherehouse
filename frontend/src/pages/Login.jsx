import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext.jsx";

export default function Login() {
  const { t } = useTranslation();
  const { login, loginCustomer, isAuthenticated, isAdmin, isCustomer } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState("staff");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [phone, setPhone] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // Клиентский вход двухшаговый: сначала телефон, затем пароль (задать/ввести).
  const [custStep, setCustStep] = useState("phone"); // phone | set | enter
  const [custName, setCustName] = useState("");
  const [custPass, setCustPass] = useState("");
  const [custPass2, setCustPass2] = useState("");

  function switchMode(m) {
    setMode(m);
    setError("");
    setCustStep("phone");
    setCustPass("");
    setCustPass2("");
  }
  function custBack() {
    setCustStep("phone");
    setCustPass("");
    setCustPass2("");
    setError("");
  }

  if (isAuthenticated) {
    navigate(isCustomer ? "/me" : isAdmin ? "/admin" : "/app", { replace: true });
  }

  async function onStaff(e) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const user = await login(username.trim(), password);
      navigate(user.role === "ADMIN" ? "/admin" : "/app", { replace: true });
    } catch {
      setError(t("login.error"));
    } finally {
      setBusy(false);
    }
  }

  async function onCustomer(e) {
    e.preventDefault();
    setError("");
    if (custStep === "set" && custPass !== custPass2) {
      setError(t("login.passMismatch"));
      return;
    }
    setBusy(true);
    try {
      const res =
        custStep === "phone"
          ? await loginCustomer(phone.trim())
          : await loginCustomer(phone.trim(), custPass);
      if (res.loggedIn) {
        navigate("/me", { replace: true });
      } else {
        setCustName(res.name || "");
        setCustStep(res.status === "set_password" ? "set" : "enter");
      }
    } catch (err) {
      setError(err?.response?.data?.detail || t("login.customerError"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <h1 style={{ color: "var(--accent-strong)" }}>{t("app.title")}</h1>
        <p className="muted" style={{ marginTop: -6 }}>{t("login.subtitle")}</p>

        <div style={{ display: "flex", gap: 8, margin: "16px 0" }}>
          <button
            type="button"
            className={mode === "staff" ? "" : "secondary"}
            style={{ flex: 1 }}
            onClick={() => switchMode("staff")}
          >
            {t("login.staffTab")}
          </button>
          <button
            type="button"
            className={mode === "customer" ? "" : "secondary"}
            style={{ flex: 1 }}
            onClick={() => switchMode("customer")}
          >
            {t("login.clientTab")}
          </button>
        </div>

        <p className="muted" style={{ fontSize: 13, marginTop: -6, marginBottom: 12 }}>
          {t("login.tabsHint")}
        </p>

        {mode === "staff" ? (
          <form onSubmit={onStaff}>
            <div className="field">
              <label>{t("common.username")}</label>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
              />
            </div>
            <div className="field">
              <label>{t("common.password")}</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </div>
            {error && <div className="error">{error}</div>}
            <button type="submit" style={{ width: "100%" }} disabled={busy}>
              {busy ? t("common.loading") : t("common.login")}
            </button>
          </form>
        ) : (
          <form onSubmit={onCustomer}>
            {custStep === "phone" ? (
              <>
                <div className="field">
                  <label>{t("clients.phone")}</label>
                  <input
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    autoFocus
                    inputMode="tel"
                    placeholder="+996 700 00 00 00"
                  />
                </div>
                <p className="muted" style={{ fontSize: 13, marginTop: -4 }}>{t("login.clientHint")}</p>
              </>
            ) : (
              <>
                <p style={{ fontWeight: 600, marginBottom: 2 }}>
                  {custStep === "set"
                    ? t("login.greetNew", { name: custName })
                    : t("login.greetBack", { name: custName })}
                </p>
                <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                  {custStep === "set" ? t("login.setPassHint") : t("login.enterPassHint")}
                </p>
                <div className="field">
                  <label>{t("common.password")}</label>
                  <input
                    type="password"
                    value={custPass}
                    onChange={(e) => setCustPass(e.target.value)}
                    autoFocus
                    autoComplete={custStep === "set" ? "new-password" : "current-password"}
                  />
                </div>
                {custStep === "set" && (
                  <div className="field">
                    <label>{t("login.passConfirm")}</label>
                    <input
                      type="password"
                      value={custPass2}
                      onChange={(e) => setCustPass2(e.target.value)}
                      autoComplete="new-password"
                    />
                  </div>
                )}
                <button
                  type="button"
                  className="ghost"
                  onClick={custBack}
                  style={{ padding: 0, fontSize: 13, color: "var(--accent-strong)" }}
                >
                  ← {t("login.otherPhone")}
                </button>
              </>
            )}
            {error && <div className="error">{error}</div>}
            <button type="submit" style={{ width: "100%", marginTop: 12 }} disabled={busy}>
              {busy ? t("common.loading") : custStep === "phone" ? t("common.next") : t("login.clientBtn")}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
