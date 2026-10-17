import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { ChevronDown, LogOut, Monitor, Moon, Search, Sun, TrendingUp } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { NAV, titleFor } from "../nav";
import { useConnection } from "../realtime/RealtimeProvider";
import { useTheme } from "../theme/ThemeProvider";
import { Button } from "../ui/Button";
import { cn } from "../ui/cn";
import { Kbd } from "../ui/fields";
import { useUI } from "../ui/UIProvider";
import { StatusPill, feedState } from "./StatusPill";

// Code for the overlays is fetched the first time they are used, keeping the first load small.
const CommandPalette = lazy(() => import("./CommandPalette"));
const OrderDrawer = lazy(() => import("./OrderDrawer"));

const isMac = typeof navigator !== "undefined" && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);

export function Layout() {
  const { user, mode } = useAuth();
  const { openPalette, paletteLoaded, orderLoaded } = useUI();
  const location = useLocation();
  const isAdmin = user?.role === "admin";
  const items = NAV.filter((n) => !n.admin || isAdmin);
  const mainRef = useRef<HTMLElement>(null);
  const first = useRef(true);

  // A route change is announced by the new page title and keyboard focus moves to the content.
  useEffect(() => {
    document.title = `${titleFor(location.pathname)} · Market Data`;
    if (first.current) {
      first.current = false;
      return;
    }
    mainRef.current?.focus({ preventScroll: false });
  }, [location.pathname]);

  // Warm the overlay code when the browser is idle, so the first Ctrl+K is instant.
  useEffect(() => {
    const idle = (window as unknown as { requestIdleCallback?: (cb: () => void) => void }).requestIdleCallback ?? ((cb: () => void) => setTimeout(cb, 2000));
    idle(() => {
      void import("./CommandPalette");
      void import("./OrderDrawer");
    });
  }, []);

  return (
    <div className="flex min-h-dvh flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-control focus:bg-brand focus:px-4 focus:py-2 focus:text-on-brand">
        Skip to content
      </a>
      <LiveAnnouncer />

      <header className="sticky top-0 z-30 border-b border-line bg-surface/90 backdrop-blur supports-[backdrop-filter]:bg-surface/75">
        <div className="mx-auto flex h-14 w-full max-w-7xl items-center gap-3 px-3 sm:px-5">
          <NavLink to="/" aria-label="Market Data home" className="flex shrink-0 items-center gap-2 rounded-control font-semibold tracking-tight">
            <span aria-hidden="true" className="grid size-8 place-items-center rounded-control bg-brand text-on-brand">
              <TrendingUp className="size-4.5" />
            </span>
            <span className="hidden sm:inline">Market Data</span>
          </NavLink>
          <span className="hidden rounded-full bg-brand-soft px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-brand-fg md:inline">Simulated</span>

          <nav aria-label="Primary" className="ml-2 hidden items-center gap-0.5 xl:flex">
            {items.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === "/"}
                className={({ isActive }) =>
                  cn(
                    "inline-flex min-h-9 items-center rounded-control px-3 text-sm font-medium transition-colors",
                    isActive ? "bg-brand-soft text-brand-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
                  )
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={openPalette}
              title="Search and commands"
              aria-keyshortcuts="Control+K Meta+K"
              className="flex min-h-9 items-center gap-2 rounded-control border border-control bg-surface px-2.5 text-sm text-muted transition-colors hover:border-fg/40 hover:text-fg sm:min-w-52"
            >
              <Search aria-hidden="true" className="size-4" />
              <span className="sr-only sm:hidden">Search</span>
              <span className="hidden flex-1 text-left sm:inline">Search</span>
              <span className="hidden gap-1 sm:flex">
                <Kbd>{isMac ? "⌘" : "Ctrl"}</Kbd>
                <Kbd>K</Kbd>
              </span>
            </button>
            <span className="hidden sm:block">
              <StatusPill />
            </span>
            <ThemeToggle />
            {mode === "required" && <UserMenu />}
          </div>
        </div>
        <div className="border-t border-line px-3 py-1.5 sm:hidden">
          <StatusPill />
        </div>
      </header>

      <main id="main" ref={mainRef} tabIndex={-1} className="mx-auto w-full max-w-7xl flex-1 px-3 pb-24 pt-4 outline-none sm:px-5 xl:pb-10 xl:pt-6">
        <div className="grid gap-4 xl:gap-5">
          <Outlet />
        </div>
      </main>

      <nav aria-label="Primary (mobile)" className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface/95 pb-[env(safe-area-inset-bottom)] backdrop-blur xl:hidden">
        <ul className="mx-auto grid max-w-xl grid-cols-5">
          {items
            .filter((n) => n.primary)
            .map((n) => (
              <li key={n.to}>
                <NavLink
                  to={n.to}
                  end={n.to === "/"}
                  className={({ isActive }) => cn("flex min-h-14 flex-col items-center justify-center gap-0.5 text-[11px] font-medium", isActive ? "text-brand-fg" : "text-muted")}
                >
                  {({ isActive }) => (
                    <>
                      <n.icon aria-hidden="true" className={cn("size-5", isActive && "stroke-[2.4]")} />
                      <span>{n.short}</span>
                    </>
                  )}
                </NavLink>
              </li>
            ))}
        </ul>
      </nav>

      <Suspense fallback={null}>
        {paletteLoaded && <CommandPalette />}
        {orderLoaded && <OrderDrawer />}
      </Suspense>
    </div>
  );
}

/** Speaks connection changes (and only changes, not ticks) to screen readers. */
function LiveAnnouncer() {
  const { status, feedAgeMs } = useConnection();
  const state = feedState(status, feedAgeMs);
  const [message, setMessage] = useState("");
  const previous = useRef(state);
  useEffect(() => {
    if (previous.current === state) return;
    previous.current = state;
    const text: Record<string, string> = {
      live: "Live data connected.",
      stale: "Market data is stale: no update for over 15 seconds.",
      reconnecting: "Connection lost. Reconnecting.",
      offline: "Offline.",
      "signed-out": "Your session ended. Please sign in again.",
      connecting: "Connecting.",
    };
    setMessage(text[state] ?? "");
  }, [state]);
  return (
    <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
      {message}
    </div>
  );
}

function ThemeToggle() {
  const { mode, cycle } = useTheme();
  const Icon = mode === "system" ? Monitor : mode === "light" ? Sun : Moon;
  const next = mode === "system" ? "light" : mode === "light" ? "dark" : "system";
  return (
    <button
      type="button"
      onClick={cycle}
      aria-label={`Theme: ${mode}. Switch to ${next}.`}
      title={`Theme: ${mode}`}
      className="grid size-9 place-items-center rounded-control border border-control bg-surface text-muted transition-colors hover:border-fg/40 hover:text-fg"
    >
      <Icon aria-hidden="true" className="size-4" />
    </button>
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const isAdmin = user?.role === "admin";

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrap.current && !wrap.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const initial = (user?.email ?? "?").slice(0, 1).toUpperCase();
  return (
    <div ref={wrap} className="relative">
      <button
        type="button"
        aria-expanded={open}
        aria-controls="user-panel"
        onClick={() => setOpen((o) => !o)}
        className="flex min-h-9 items-center gap-1.5 rounded-control border border-control bg-surface pl-1 pr-2 text-sm hover:border-fg/40"
      >
        <span aria-hidden="true" className="grid size-7 place-items-center rounded-full bg-brand-soft text-xs font-semibold text-brand-fg">
          {initial}
        </span>
        <span className="sr-only">Account menu</span>
        <ChevronDown aria-hidden="true" className="size-3.5 text-muted" />
      </button>
      {open && (
        <div id="user-panel" className="absolute right-0 top-11 z-40 w-64 rounded-card border border-line bg-surface p-2 shadow-pop">
          <div className="px-2 py-1.5">
            <p className="truncate text-sm font-medium">{user?.email}</p>
            <p className="text-xs text-muted">{isAdmin ? "Administrator" : "Member"}</p>
          </div>
          {isAdmin && (
            <div className="my-1 grid border-t border-line pt-1">
              {NAV.filter((n) => n.admin).map((n) => (
                <button
                  key={n.to}
                  type="button"
                  className="flex min-h-9 items-center gap-2 rounded-control px-2 text-sm text-fg hover:bg-surface-2"
                  onClick={() => {
                    setOpen(false);
                    navigate(n.to);
                  }}
                >
                  <n.icon aria-hidden="true" className="size-4 text-muted" />
                  {n.label}
                </button>
              ))}
            </div>
          )}
          <div className="border-t border-line pt-1">
            <Button
              variant="ghost"
              className="w-full justify-start"
              onClick={async () => {
                setOpen(false);
                await logout();
                navigate("/login", { replace: true });
              }}
            >
              <LogOut aria-hidden="true" className="size-4" />
              Sign out
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
