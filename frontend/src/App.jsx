import { Navigate, Route, Routes } from "react-router-dom";

import FinanceGate from "./components/FinanceGate.jsx";
import Layout from "./components/Layout.jsx";
import ProtectedRoute from "./components/ProtectedRoute.jsx";
import { useAuth } from "./auth/AuthContext.jsx";

import Login from "./pages/Login.jsx";
import Dashboard from "./pages/admin/Dashboard.jsx";
import Stock from "./pages/admin/Stock.jsx";
import Pricing from "./pages/admin/Pricing.jsx";
import Clients from "./pages/admin/Clients.jsx";
import ReferralRequests from "./pages/admin/ReferralRequests.jsx";
import Receipts from "./pages/admin/Receipts.jsx";
import FinanceSection from "./pages/admin/FinanceSection.jsx";
import CustomerOrders from "./pages/customer/CustomerOrders.jsx";
import Warehouse from "./pages/store/Warehouse.jsx";
import Checkout from "./pages/store/Checkout.jsx";
import StoreReceipts from "./pages/store/StoreReceipts.jsx";

const ADMIN_NAV = [
  {
    section: "nav.sectionDaily",
    items: [
      { to: "/admin", label: "nav.checkout", end: true, icon: "cart" },
      { to: "/admin/receipts", label: "nav.receipts", icon: "receipt" },
      { to: "/admin/clients", label: "nav.clients", icon: "users" },
      { to: "/admin/catalog", label: "nav.warehouse", icon: "package" },
      { to: "/admin/finance", label: "nav.finance", icon: "clipboard" },
    ],
  },
  {
    section: "nav.sectionRare",
    items: [
      { to: "/admin/dashboard", label: "nav.dashboard", icon: "dashboard" },
      { to: "/admin/pricing", label: "nav.pricing", icon: "tag" },
      { to: "/admin/referral-requests", label: "nav.referralRequests", icon: "shuffle" },
    ],
  },
];

const STORE_NAV = [
  {
    items: [
      { to: "/app", label: "nav.warehouse", end: true, icon: "package" },
      { to: "/app/checkout", label: "nav.checkout", icon: "cart" },
      { to: "/app/clients", label: "nav.clients", icon: "users" },
      { to: "/app/receipts", label: "nav.receipts", icon: "receipt" },
    ],
  },
];

const CUSTOMER_NAV = [
  {
    items: [{ to: "/me", label: "nav.myOrders", end: true, icon: "receipt" }],
  },
];

export default function App() {
  const { isAuthenticated, isAdmin, isCustomer } = useAuth();

  const home = !isAuthenticated ? "/login" : isCustomer ? "/me" : isAdmin ? "/admin" : "/app";

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
        <Route path="/admin/referral-requests" element={<ReferralRequests />} />
        <Route path="/admin/receipts" element={<Receipts />} />
        <Route
          path="/admin/finance"
          element={
            <FinanceGate>
              <FinanceSection />
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
