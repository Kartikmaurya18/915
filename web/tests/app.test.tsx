import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { AllProviders } from "./providers";
import { getAccessToken, setAccessToken } from "../src/api/http";
import { RealtimeClient } from "../src/realtime/client";
import { RealtimeProvider } from "../src/realtime/RealtimeProvider";
import { FakeSocket } from "./fakeSocket";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;
function mockFetch(handlers: Record<string, Handler>) {
  const fn = vi.fn(async (url: string, init: RequestInit = {}) => {
    const key = `${init.method ?? "GET"} ${url.split("?")[0]}`;
    const h = handlers[key];
    if (!h) return json(404, { detail: `unmocked ${key}` });
    return h(url, init);
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function mount(path: string) {
  const client = new RealtimeClient({
    url: () => "ws://test/ws/v1",
    authRequired: () => true,
    getToken: getAccessToken,
    WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
  });
  // App renders its own RealtimeProvider; the injected client is used through the prop on a wrapper.
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AllProviders>
        <InjectedApp client={client} />
      </AllProviders>
    </MemoryRouter>,
  );
}

// Re-use App's route table but with a client that talks to the fake socket.
function InjectedApp({ client }: { client: RealtimeClient }) {
  return (
    <RealtimeProvider client={client}>
      <AppRoutes />
    </RealtimeProvider>
  );
}
import { Route, Routes } from "react-router-dom";
import { Layout } from "../src/components/Layout";
import { RedirectIfAuthed, RequireAdmin, RequireAuth } from "../src/components/guards";
import { AuthPage } from "../src/pages/AuthPage";
function AppRoutes() {
  return (
    <Routes>
      <Route element={<RedirectIfAuthed />}>
        <Route path="/login" element={<AuthPage kind="login" />} />
      </Route>
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route index element={<p>overview-page</p>} />
          <Route element={<RequireAdmin />}>
            <Route path="logs" element={<p>logs-page</p>} />
          </Route>
        </Route>
      </Route>
    </Routes>
  );
}

const userSession = (role: "user" | "admin") => ({
  access_token: "access-1",
  token_type: "bearer",
  expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
  user: { id: "u1", email: "a@b.co", role },
});

beforeEach(() => {
  FakeSocket.reset();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe("auth flow", () => {
  it("sends an anonymous visitor to the login page without calling /refresh", async () => {
    const fetchFn = mockFetch({
      "GET /api/auth/me": () => json(401, { detail: { code: "not_authenticated", message: "Authentication required." } }),
    });
    mount("/logs");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect(fetchFn.mock.calls.some(([u]) => String(u).includes("/refresh"))).toBe(false);
  });

  it("signs in, lands on the app, and opens the realtime socket with the token", async () => {
    mockFetch({
      "GET /api/auth/me": () => json(401, { detail: { code: "not_authenticated", message: "x" } }),
      "POST /api/auth/login": () => json(200, userSession("user")),
    });
    mount("/login");
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "a@b.co" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct horse battery" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("overview-page")).toBeTruthy();
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
    FakeSocket.last.open();
    expect(FakeSocket.last.sent[0]).toEqual({ type: "auth", token: "access-1" });
    expect(localStorage.getItem("md_session")).toBe("1");
  });

  it("shows the server's message on a failed login and stays on the form", async () => {
    mockFetch({
      "GET /api/auth/me": () => json(401, { detail: { code: "not_authenticated", message: "x" } }),
      "POST /api/auth/login": () => json(401, { detail: { code: "invalid_credentials", message: "Incorrect email or password." } }),
    });
    mount("/login");
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "a@b.co" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong-password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Incorrect email or password.");
    expect(FakeSocket.instances.length).toBe(0);
  });

  it("resumes a remembered session through the refresh cookie", async () => {
    localStorage.setItem("md_session", "1");
    mockFetch({
      "GET /api/auth/me": () => json(401, { detail: { code: "not_authenticated", message: "x" } }),
      "POST /api/auth/refresh": () => json(200, userSession("user")),
    });
    mount("/");
    expect(await screen.findByText("overview-page")).toBeTruthy();
  });

  it("returns to the login page when the remembered session is no longer valid", async () => {
    localStorage.setItem("md_session", "1");
    mockFetch({
      "GET /api/auth/me": () => json(401, { detail: { code: "not_authenticated", message: "x" } }),
      "POST /api/auth/refresh": () => json(401, { detail: { code: "refresh_token_reused", message: "Session is no longer valid." } }),
    });
    mount("/");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect(localStorage.getItem("md_session")).toBeNull();
  });

  it("a remembered session on a server that has auth disabled falls back to local mode", async () => {
    localStorage.setItem("md_session", "1");
    mockFetch({ "POST /api/auth/refresh": () => json(404, { detail: "Not found" }) });
    mount("/");
    expect(await screen.findByText("overview-page")).toBeTruthy();
  });

  it("a rate-limited or unreachable refresh keeps the remembered session instead of forgetting it", async () => {
    localStorage.setItem("md_session", "1");
    mockFetch({ "POST /api/auth/refresh": () => json(429, { detail: { code: "rate_limited", message: "Too many attempts. Try again later." } }) });
    mount("/");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
    expect(localStorage.getItem("md_session")).toBe("1");
  });

  it("works without any sign-in when the server runs with auth disabled", async () => {
    mockFetch({ "GET /api/auth/me": () => json(404, { detail: "Not found" }) });
    mount("/");
    expect(await screen.findByText("overview-page")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });
});

describe("role-based navigation", () => {
  async function signedIn(role: "user" | "admin") {
    localStorage.setItem("md_session", "1");
    mockFetch({
      "GET /api/auth/me": () => json(401, { detail: { code: "not_authenticated", message: "x" } }),
      "POST /api/auth/refresh": () => json(200, userSession(role)),
    });
  }

  it("hides admin links and blocks the page for a normal user", async () => {
    await signedIn("user");
    mount("/logs");
    expect(await screen.findByText("Admin only")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Logs" })).toBeNull();
  });

  it("shows admin links and the page for an admin", async () => {
    await signedIn("admin");
    mount("/logs");
    expect(await screen.findByText("logs-page")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Logs" })).toBeTruthy();
  });
});

describe("App", () => {
  it("renders the real route table", async () => {
    mockFetch({ "GET /api/auth/me": () => json(401, { detail: { code: "not_authenticated", message: "x" } }) });
    render(
      <MemoryRouter initialEntries={["/trading"]}>
        <AllProviders>
          <App />
        </AllProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeTruthy();
  });
});
