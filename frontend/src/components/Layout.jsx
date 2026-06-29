import { useState } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthContext.jsx";
import Icon from "./Icon.jsx";
import LanguageSwitcher from "./LanguageSwitcher.jsx";

export default function Layout({ nav }) {
  const { t } = useTranslation();
  const { user, logout, isAdmin, isCustomer } = useAuth();
  const [open, setOpen] = useState(false);

  return (
    <div className="shell">
      {open && <div className="overlay" onClick={() => setOpen(false)} />}
      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="brand">{t("app.title")}</div>
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
      </aside>

      <div className="main">
        <header className="topbar">
          <button className="burger" onClick={() => setOpen((v) => !v)} aria-label="menu">
            <Icon name="menu" size={22} />
          </button>
          <strong>{isCustomer ? t("roles.customer") : isAdmin ? t("roles.admin") : t("roles.storekeeper")}</strong>
          <div className="spacer" />
          <LanguageSwitcher />
          <span className="muted" style={{ marginLeft: 4 }}>
            {user?.username || user?.name}
          </span>
          <button className="secondary" onClick={logout}>
            {t("common.logout")}
          </button>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
