import { lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import FinanceGate from "./components/FinanceGate.jsx";
import Layout from "./components/Layout.jsx";
import ProtectedRoute from "./components/ProtectedRoute.jsx";
import { useAuth } from "./auth/AuthContext.jsx";

import Login from "./pages/Login.jsx";

// Экраны грузятся по требованию: раньше всё приложение приезжало одним файлом
// в 808 кБ (245 кБ в сжатом виде), хотя кассиру нужна касса, а бухгалтеру —
// чеки и финансы. Вход остаётся в основном файле — он первый экран любого
// пользователя. Запасной «Загрузка…» показывает Layout (Suspense вокруг Outlet).
const Dashboard = lazy(() => import("./pages/admin/Dashboard.jsx"));
const Stock = lazy(() => import("./pages/admin/Stock.jsx"));
const Pricing = lazy(() => import("./pages/admin/Pricing.jsx"));
const Cash = lazy(() => import("./pages/admin/Cash.jsx"));
const Clients = lazy(() => import("./pages/admin/Clients.jsx"));
const Receipts = lazy(() => import("./pages/admin/Receipts.jsx"));
const Supplies = lazy(() => import("./pages/admin/Supplies.jsx"));
const Finance = lazy(() => import("./pages/admin/Finance.jsx"));
const CustomerOrders = lazy(() => import("./pages/customer/CustomerOrders.jsx"));
const Warehouse = lazy(() => import("./pages/store/Warehouse.jsx"));
const Checkout = lazy(() => import("./pages/store/Checkout.jsx"));
const StoreReceipts = lazy(() => import("./pages/store/StoreReceipts.jsx"));

const ADMIN_NAV = [
  {
    section: "nav.sectionDaily",
    items: [
      { to: "/admin", label: "nav.checkout", end: true, icon: "cart" },
      { to: "/admin/receipts", label: "nav.receipts", icon: "receipt" },
      { to: "/admin/clients", label: "nav.clients", icon: "users" },
      { to: "/admin/catalog", label: "nav.warehouse", icon: "package" },
      { to: "/admin/finance", label: "nav.finance", icon: "clipboard" },
      { to: "/admin/cash", label: "nav.cash", icon: "wallet" },
    ],
  },
  {
    section: "nav.sectionRare",
    items: [
      { to: "/admin/dashboard", label: "nav.dashboard", icon: "dashboard" },
      { to: "/admin/pricing", label: "nav.pricing", icon: "tag" },
    ],
  },
];

const STORE_NAV = [
  {
    items: [
      { to: "/app/checkout", label: "nav.checkout", icon: "cart" },
      { to: "/app/receipts", label: "nav.receipts", icon: "receipt" },
      { to: "/app/clients", label: "nav.clients", icon: "users" },
      { to: "/app", label: "nav.warehouse", end: true, icon: "package" },
      // Приход накладной — работа складовщика: он принимает товар и выдаёт
      // приходную накладную. Тот же экран, что у админа во вкладке «Приходы».
      { to: "/app/supplies", label: "nav.supply", icon: "inbox" },
    ],
  },
];

// Бухгалтер: проверяет, а не участвует. Чеки с себестоимостью и маржой, журнал
// действий (он же вкладка на экране чеков), финансовый отчёт и обзор — всё
// только на просмотр. Кассы, склада и клиентов у него нет ни в меню, ни на
// сервере.
const ACCOUNTANT_NAV = [
  {
    items: [
      { to: "/acc", label: "nav.receipts", end: true, icon: "receipt" },
      { to: "/acc/finance", label: "nav.finance", icon: "clipboard" },
      { to: "/acc/cash", label: "nav.cash", icon: "wallet" },
      { to: "/acc/dashboard", label: "nav.dashboard", icon: "dashboard" },
    ],
  },
];

const CUSTOMER_NAV = [
  {
    items: [{ to: "/me", label: "nav.myOrders", end: true, icon: "receipt" }],
  },
];

export default function App() {
  const { isAuthenticated, isAdmin, isAccountant, isCustomer } = useAuth();

  const home = !isAuthenticated
    ? "/login"
    : isCustomer
    ? "/me"
    : isAccountant
    ? "/acc"
    : isAdmin
    ? "/admin"
    : "/app/checkout";

  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* Admin area */}
      <Route
        element={
          <ProtectedRoute requireAdmin>
            <Layout nav={ADMIN_NAV} />
          </ProtectedRoute>
        }
      >
        <Route path="/admin" element={<Checkout />} />
        <Route
          path="/admin/dashboard"
          element={
            <FinanceGate>
              <Dashboard />
            </FinanceGate>
          }
        />
        <Route path="/admin/catalog" element={<Stock />} />
        <Route path="/admin/supply" element={<Navigate to="/admin/catalog?tab=movement" replace />} />
        <Route path="/admin/pricing" element={<Pricing />} />
        <Route path="/admin/clients" element={<Clients />} />
        <Route path="/admin/receipts" element={<Receipts />} />
        <Route
          path="/admin/finance"
          element={
            <FinanceGate>
              <Finance />
            </FinanceGate>
          }
        />
        <Route
          path="/admin/cash"
          element={
            <FinanceGate>
              <Cash />
            </FinanceGate>
          }
        />
        <Route path="/admin/expenses" element={<Navigate to="/admin/finance" replace />} />
      </Route>

      {/* Storekeeper area */}
      <Route
        element={
          <ProtectedRoute>
            <Layout nav={STORE_NAV} />
          </ProtectedRoute>
        }
      >
        <Route path="/app" element={<Warehouse />} />
        <Route path="/app/checkout" element={<Checkout />} />
        <Route path="/app/clients" element={<Clients />} />
        <Route path="/app/receipts" element={<StoreReceipts />} />
        <Route path="/app/supplies" element={<Supplies />} />
      </Route>

      {/* Бухгалтер — отдельный раздел, всё только на просмотр */}
      <Route
        element={
          <ProtectedRoute requireAccountant>
            <Layout nav={ACCOUNTANT_NAV} />
          </ProtectedRoute>
        }
      >
        <Route path="/acc" element={<Receipts />} />
        <Route
          path="/acc/finance"
          element={
            <FinanceGate>
              <Finance />
            </FinanceGate>
          }
        />
        <Route
          path="/acc/cash"
          element={
            <FinanceGate>
              <Cash />
            </FinanceGate>
          }
        />
        <Route
          path="/acc/dashboard"
          element={
            <FinanceGate>
              <Dashboard />
            </FinanceGate>
          }
        />
      </Route>

      {/* Customer self-service portal */}
      <Route
        element={
          <ProtectedRoute requireCustomer>
            <Layout nav={CUSTOMER_NAV} />
          </ProtectedRoute>
        }
      >
        <Route path="/me" element={<CustomerOrders />} />
      </Route>

      <Route path="*" element={<Navigate to={home} replace />} />
    </Routes>
  );
}
