import { Suspense, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext.jsx";
import ErrorBoundary from "./ErrorBoundary.jsx";
import Icon from "./Icon.jsx";
import ThemeSwitcher from "./ThemeSwitcher.jsx";
import LanguageSwitcher from "./LanguageSwitcher.jsx";

export default function Layout({ nav }) {
  const { t } = useTranslation();
  const { user, logout, isAdmin, isAccountant, isCustomer } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const burger = useRef(null);
  const sidebar = useRef(null);
  const main = useRef(null);

  // Один пункт меню (кабинет клиента) — боковая панель и бургер ничего не
  // дают; название системы переезжает в шапку.
  const single = nav.reduce((n, g) => n + g.items.length, 0) <= 1;

  // Esc закрывает открытое мобильное меню и возвращает фокус на бургер; при
  // открытии фокус уходит на первый пункт.
  useEffect(() => {
    if (!open) return undefined;
    sidebar.current?.querySelector("a")?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") {
        setOpen(false);
        burger.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  function skipToContent(e) {
    e.preventDefault();
    main.current?.focus();
    main.current?.scrollIntoView?.();
  }

  return (
    <div className={`shell${single ? " no-sidebar" : ""}`}>
      <a className="skip-link" href="#main-content" onClick={skipToContent}>
        {t("common.skipToContent")}
      </a>
      {open && <div className="overlay" onClick={() => setOpen(false)} />}
      <aside className={`sidebar ${open ? "open" : ""}`} ref={sidebar}>
        <div className="brand">{t("app.title")}</div>
        <nav aria-label={t("nav.main")}>
          {nav.map((group, gi) => (
            <div className="nav-group" key={group.section || gi}>
              {group.section && <div className="nav-section">{t(group.section)}</div>}
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) => (isActive ? "active" : "")}
                  onClick={() => setOpen(false)}
                >
                  {item.icon && <Icon name={item.icon} size={18} className="nav-icon" />}
                  {t(item.label)}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            ref={burger}
            className="burger"
            onClick={() => setOpen((v) => !v)}
            aria-label={t("common.menu")}
            aria-expanded={open}
          >
            <Icon name="menu" size={22} />
          </button>
          <span className="topbar-brand">{t("app.title")}</span>
          <strong>
            {isCustomer
              ? t("roles.customer")
              : isAdmin
              ? t("roles.admin")
              : isAccountant
              ? t("roles.accountant")
              : t("roles.storekeeper")}
          </strong>
          <div className="spacer" />
          <ThemeSwitcher />
          <LanguageSwitcher />
          <span className="muted" style={{ marginLeft: 4 }}>
            {user?.username || user?.name}
          </span>
          <button className="secondary logout-btn" onClick={logout} aria-label={t("common.logout")}>
            <Icon name="log-out" size={18} className="logout-icon" />
            <span className="logout-text">{t("common.logout")}</span>
          </button>
        </header>
        <main className="content" id="main-content" tabIndex={-1} ref={main}>
          {/* Сбой одного экрана не должен ронять шапку и меню; переход на
              другой экран сбрасывает предохранитель (key). */}
          <ErrorBoundary key={location.pathname} onHome={() => navigate("/")}>
            <Suspense fallback={<p className="muted route-loading" role="status">{t("common.loading")}</p>}>
              <Outlet />
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
