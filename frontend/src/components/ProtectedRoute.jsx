import { Navigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext.jsx";

export default function ProtectedRoute({ children, requireAdmin = false, requireCustomer = false }) {
  const { isAuthenticated, isAdmin, isCustomer } = useAuth();

  if (!isAuthenticated) return <Navigate to="/login" replace />;

  if (requireCustomer) {
    return isCustomer ? children : <Navigate to={isAdmin ? "/admin" : "/app"} replace />;
  }
  // A customer may only use the customer portal.
  if (isCustomer) return <Navigate to="/me" replace />;
  if (requireAdmin && !isAdmin) return <Navigate to="/app" replace />;
  return children;
}
