import { Navigate, Outlet, useLocation } from "react-router-dom";
import { Lock } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { EmptyState } from "../ui/feedback";

export function Splash({ message = "Loading…" }: { message?: string }) {
  return (
    <div role="status" className="grid min-h-[40dvh] place-items-center text-sm text-muted">
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
      <div className="rounded-card border border-line bg-surface shadow-card">
        <EmptyState title="Admin only" icon={<Lock className="size-5" />}>
          Your account does not have access to this page.
        </EmptyState>
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
