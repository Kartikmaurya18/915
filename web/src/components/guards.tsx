import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function Splash({ message = "Loading…" }: { message?: string }) {
  return (
    <div className="splash" role="status">
      {message}
    </div>
  );
}

/** Everything inside needs a signed-in user (or a server running with auth disabled). */
export function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <Splash />;
  if (status === "anonymous") return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}

/** UI courtesy only: the API enforces the role on every admin endpoint. */
export function RequireAdmin() {
  const { user } = useAuth();
  if (user?.role !== "admin") {
    return (
      <div className="panel notice">
        <h2>Admin only</h2>
        <p>Your account does not have access to this page.</p>
      </div>
    );
  }
  return <Outlet />;
}

/** Login / register: bounce to the app if already signed in. */
export function RedirectIfAuthed() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "loading") return <Splash />;
  if (status === "authenticated") {
    const from = (location.state as { from?: string } | null)?.from;
    return <Navigate to={from && from !== "/login" ? from : "/"} replace />;
  }
  return <Outlet />;
}
