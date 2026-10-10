import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useNavigate } from "react-router-dom";

import { failureKind } from "../api/errors.js";
import Field, { focusFirstInvalid } from "../components/Field.jsx";
import LanguageSwitcher from "../components/LanguageSwitcher.jsx";
import ThemeSwitcher from "../components/ThemeSwitcher.jsx";
import { useAuth } from "../auth/AuthContext.jsx";

export default function Login() {
  const { t } = useTranslation();
  const { login, loginCustomer, isAuthenticated, isAdmin, isAccountant, isCustomer } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState("staff");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [phone, setPhone] = useState("");
  const [error, setError] = useState("");
  // Ошибки валидации — рядом с полем, а не одной строкой над кнопкой:
  // {username, password, phone, custPass}. Общая ошибка (неверный пароль,
  // сервер недоступен) остаётся в `error`.
  const [fieldErr, setFieldErr] = useState({});
  const [busy, setBusy] = useState(false);
  // Клиентский вход двухшаговый: сначала телефон, затем пароль (задать/ввести).
  const [custStep, setCustStep] = useState("phone"); // phone | set | enter
  const [custPass, setCustPass] = useState("");
  const [custPass2, setCustPass2] = useState("");

  // Текст общей ошибки входа по ВИДУ сбоя, а не по тексту сервера: detail
  // приходит по-русски, и кыргызоязычный или англоязычный сотрудник видел бы
  // чужой язык. Неверный пароль — 400/401; 429 — предел попыток (говорить
  // «неверный пароль» здесь было бы враньём); всё остальное — сервер/сеть.
  function loginFailure(err, wrongCredentials) {
    switch (failureKind(err)) {
      case "rate":
        return t("errors.rateLimit");
      case "timeout":
      case "network":
      case "server":
        return t("errors.server");
      default:
        return wrongCredentials;
    }
  }

  function switchMode(m) {
    setMode(m);
    setError("");
    setFieldErr({});
    setCustStep("phone");
    setCustPass("");
    setCustPass2("");
  }
  function custBack() {
    setCustStep("phone");
    setCustPass("");
    setCustPass2("");
    setError("");
    setFieldErr({});
  }

  // Уже вошёл — на свою главную. Раньше navigate() вызывался прямо во время
  // отрисовки (React ругался «Cannot update a component while rendering»).
  if (isAuthenticated) {
    return <Navigate to={isCustomer ? "/me" : isAdmin ? "/admin" : isAccountant ? "/acc" : "/app"} replace />;
  }

  function fail(errs) {
    setFieldErr(errs);
    focusFirstInvalid();
  }

  async function onStaff(e) {
    e.preventDefault();
    setError("");
    setFieldErr({});
    // Пустые поля отправлять некуда: сервер ответит «неверный логин или
    // пароль», хотя ничего не вводили, — и потратит попытку из предела 10/мин
    // на адрес. Десять таких нажатий запирали вход всей кассе.
    const errs = {};
    if (!username.trim()) errs.username = t("login.needUsername");
    if (!password) errs.password = t("login.needPassword");
    if (errs.username || errs.password) return fail(errs);
    setBusy(true);
    try {
      const user = await login(username.trim(), password);
      navigate(user.role === "ADMIN" ? "/admin" : user.role === "ACCOUNTANT" ? "/acc" : "/app", { replace: true });
    } catch (err) {
      setError(loginFailure(err, t("login.error")));
    } finally {
      setBusy(false);
    }
  }

  async function onCustomer(e) {
    e.preventDefault();
    setError("");
    setFieldErr({});
    if (custStep === "phone" && !phone.trim()) return fail({ phone: t("login.needPhone") });
    if (custStep === "enter" && !custPass) return fail({ custPass: t("login.needPassword") });
    setBusy(true);
    try {
      const res =
        custStep === "phone"
          ? await loginCustomer(phone.trim())
          : await loginCustomer(phone.trim(), custPass);
      if (res.loggedIn) {
        navigate("/me", { replace: true });
      } else {
        // Имени тут больше нет: портал открыт на публичном домене, и раньше по
        // одному номеру он отвечал «С возвращением, Бакыт Осмонов!» — перебором
        // номеров с этой страницы собиралась клиентская база цеха.
        setCustStep("enter");
      }
    } catch (err) {
      setError(loginFailure(err, t("login.customerError")));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card">
        {/* Язык переключался только в шапке, доступной вошедшему: для
            кыргызоязычного сотрудника первый экран системы был на чужом языке. */}
        {/* Тема — здесь же: экран входа человек видит первым, и если система
            тёмная, а вход белый, это выглядит как чужое приложение. */}
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginBottom: 4 }}>
          <ThemeSwitcher />
          <LanguageSwitcher />
        </div>
        <h1 style={{ color: "var(--accent-ink)" }}>{t("app.title")}</h1>
        <p className="muted" style={{ marginTop: -6 }}>{t("login.subtitle")}</p>

        <div style={{ display: "flex", gap: 8, margin: "16px 0" }} role="group" aria-label={t("login.title")}>
          <button
            type="button"
            className={mode === "staff" ? "" : "secondary"}
            style={{ flex: 1 }}
            aria-pressed={mode === "staff"}
            onClick={() => switchMode("staff")}
          >
            {t("login.staffTab")}
          </button>
          <button
            type="button"
            className={mode === "customer" ? "" : "secondary"}
            style={{ flex: 1 }}
            aria-pressed={mode === "customer"}
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
            <Field label={t("common.username")} required error={fieldErr.username}>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
              />
            </Field>
            <Field label={t("common.password")} required error={fieldErr.password}>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </Field>
            {error && <div className="error" role="alert">{error}</div>}
            <button type="submit" style={{ width: "100%" }} disabled={busy}>
              {busy ? t("common.loading") : t("common.login")}
            </button>
          </form>
        ) : (
          <form onSubmit={onCustomer}>
            {custStep === "phone" ? (
              <>
                <Field
                  label={t("clients.phone")}
                  required
                  error={fieldErr.phone}
                  hint={t("login.clientHint")}
                >
                  <input
                    type="tel"
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    autoFocus
                    inputMode="tel"
                    autoComplete="tel"
                    placeholder="+996 700 00 00 00"
                  />
                </Field>
              </>
            ) : (
              <>
                <p style={{ fontWeight: 600, marginBottom: 2 }}>
                  {t("login.enterPassTitle")}
                </p>
                <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
                  {t("login.enterPassHint")}
                </p>
                {custStep !== "ask_admin" && (
                  <Field label={t("common.password")} required error={fieldErr.custPass}>
                    <input
                      type="password"
                      value={custPass}
                      onChange={(e) => setCustPass(e.target.value)}
                      autoFocus
                      autoComplete="current-password"
                    />
                  </Field>
                )}
                <button
                  type="button"
                  className="ghost"
                  onClick={custBack}
                  style={{ padding: 0, fontSize: 13, color: "var(--accent-ink)" }}
                >
                  ← {t("login.otherPhone")}
                </button>
              </>
            )}
            {error && <div className="error" role="alert">{error}</div>}
            {/* Без выданного пароля отправлять нечего — кнопку прячем. */}
            {custStep !== "ask_admin" && (
              <button type="submit" style={{ width: "100%", marginTop: 12 }} disabled={busy}>
                {busy ? t("common.loading") : custStep === "phone" ? t("common.next") : t("login.clientBtn")}
              </button>
            )}
          </form>
        )}
      </div>
    </div>
  );
}
