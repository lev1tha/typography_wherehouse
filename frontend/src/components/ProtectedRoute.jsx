import { Navigate } from "react-router-dom";

import { useAuth } from "../auth/AuthContext.jsx";

export default function ProtectedRoute({
  children,
  requireAdmin = false,
  requireCustomer = false,
  requireAccountant = false,
}) {
  const { isAuthenticated, isAdmin, isAccountant, isCustomer } = useAuth();

  if (!isAuthenticated) return <Navigate to="/login" replace />;

  if (requireCustomer) {
    return isCustomer ? children : <Navigate to={isAdmin ? "/admin" : "/app"} replace />;
  }
  // A customer may only use the customer portal.
  if (isCustomer) return <Navigate to="/me" replace />;

  if (requireAccountant) {
    return isAccountant ? children : <Navigate to={isAdmin ? "/admin" : "/app"} replace />;
  }
  // Бухгалтер живёт в своём разделе: касса и склад ему закрыты и на сервере,
  // так что пускать его на эти экраны значило бы показать пустые таблицы и
  // ошибки прав вместо понятного «этого раздела у вас нет».
  if (isAccountant) return <Navigate to="/acc" replace />;
  if (requireAdmin && !isAdmin) return <Navigate to="/app" replace />;
  return children;
}
