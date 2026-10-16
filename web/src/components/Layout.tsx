import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { StatusPill } from "./StatusPill";

const NAV: { to: string; label: string; admin?: boolean }[] = [
  { to: "/", label: "Overview" },
  { to: "/trading", label: "Paper Trading" },
  { to: "/rrg", label: "RRG" },
  { to: "/quality", label: "Data Quality" },
  { to: "/logs", label: "Logs", admin: true },
  { to: "/settings", label: "Settings", admin: true },
];

export function Layout() {
  const { user, mode, logout } = useAuth();
  const navigate = useNavigate();
  const isAdmin = user?.role === "admin";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">▲</span>
          <span>Market Data</span>
          <span className="tag">Paper only</span>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV.filter((n) => !n.admin || isAdmin).map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"} className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="topbar-right">
          <StatusPill />
          {mode === "required" && (
            <button
              className="btn btn-quiet"
              onClick={async () => {
                await logout();
                navigate("/login", { replace: true });
              }}
              title={user?.email}
            >
              Sign out
            </button>
          )}
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
