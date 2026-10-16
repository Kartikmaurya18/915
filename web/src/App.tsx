import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { RedirectIfAuthed, RequireAdmin, RequireAuth, Splash } from "./components/guards";
import { RealtimeProvider } from "./realtime/RealtimeProvider";
import { AuthPage } from "./pages/AuthPage";

// Route-level code splitting: the chart library only loads with the Overview page.
const Overview = lazy(() => import("./pages/Overview"));
const Trading = lazy(() => import("./pages/Trading"));
const Rrg = lazy(() => import("./pages/Rrg"));
const Quality = lazy(() => import("./pages/Quality"));
const Logs = lazy(() => import("./pages/Logs"));
const Settings = lazy(() => import("./pages/Settings"));

function NotFound() {
  return (
    <div className="panel notice">
      <h2>Page not found</h2>
      <p>That address does not exist.</p>
    </div>
  );
}

export function App() {
  return (
    <RealtimeProvider>
      <Suspense fallback={<Splash />}>
        <Routes>
          <Route element={<RedirectIfAuthed />}>
            <Route path="/login" element={<AuthPage kind="login" />} />
            <Route path="/register" element={<AuthPage kind="register" />} />
          </Route>
          <Route element={<RequireAuth />}>
            <Route element={<Layout />}>
              <Route index element={<Overview />} />
              <Route path="trading" element={<Trading />} />
              <Route path="rrg" element={<Rrg />} />
              <Route path="quality" element={<Quality />} />
              <Route element={<RequireAdmin />}>
                <Route path="logs" element={<Logs />} />
                <Route path="settings" element={<Settings />} />
              </Route>
              <Route path="*" element={<NotFound />} />
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </RealtimeProvider>
  );
}
