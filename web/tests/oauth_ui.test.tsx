import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../src/auth/AuthContext";
import { oauthErrorMessage } from "../src/lib/oauthErrors";
import OAuthComplete from "../src/pages/OAuthComplete";
import { AuthPage } from "../src/pages/AuthPage";
import { AllProviders } from "./providers";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const SESSION = { access_token: "at", token_type: "bearer", expires_at: new Date(Date.now() + 900_000).toISOString(), user: { id: "u1", email: "a@example.com", role: "user" } };

let options: Record<string, unknown>;
let refreshResponse: () => Response;
let calls: string[];

beforeEach(() => {
  localStorage.clear();
  calls = [];
  options = { verification: "off", channels: [], registration_open: true, code_length: 6, oauth: [{ id: "google", label: "Google" }] };
  refreshResponse = () => json(200, SESSION);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const path = url.split("?")[0]!;
      calls.push(path);
      if (path === "/api/auth/me") return json(401, { detail: { code: "not_authenticated", message: "x" } });
      if (path === "/api/auth/options") return json(200, options);
      if (path === "/api/auth/refresh") return refreshResponse();
      return json(404, { detail: "unmocked " + path });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Where() {
  const loc = useLocation();
  const { status } = useAuth();
  return (
    <>
      <span data-testid="where">{loc.pathname + loc.search}</span>
      <span data-testid="status">{status}</span>
    </>
  );
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AllProviders>
        <Where />
        <Routes>
          <Route path="/login" element={<AuthPage kind="login" />} />
          <Route path="/register" element={<AuthPage kind="register" />} />
          <Route path="/oauth/complete" element={<OAuthComplete />} />
          <Route path="/" element={<span>home</span>} />
        </Routes>
      </AllProviders>
    </MemoryRouter>,
  );
}

describe("messages", () => {
  it("has plain-language text for every code the server can send", () => {
    for (const code of ["oauth_denied", "oauth_expired", "oauth_email_unverified", "oauth_email_missing", "oauth_account_disabled", "oauth_registration_closed", "oauth_failed", "rate_limited"]) {
      expect(oauthErrorMessage(code), code).toBeTruthy();
    }
  });
  it("never turns an unknown code, or an object property name, into text", () => {
    for (const code of ["<script>alert(1)</script>", "toString", "__proto__", "constructor", "hasOwnProperty", "", null, undefined]) {
      expect(oauthErrorMessage(code as string | null)).toBeNull();
    }
  });
});

describe("the sign-in buttons", () => {
  it("offers Continue with Google on sign-in and sign-up, as a plain link to the server", async () => {
    renderAt("/login");
    const link = await screen.findByRole("link", { name: "Continue with Google" });
    expect(link.getAttribute("href")).toBe("/api/auth/oauth/google/start");
    cleanup();
    renderAt("/register");
    expect((await screen.findByRole("link", { name: "Continue with Google" })).getAttribute("href")).toBe("/api/auth/oauth/google/start");
  });

  it("offers nothing when no provider is configured", async () => {
    options = { ...options, oauth: [] };
    renderAt("/login");
    await screen.findByLabelText("Email");
    await waitFor(() => expect(calls).toContain("/api/auth/options"));
    expect(screen.queryByRole("link", { name: /Continue with/ })).toBeNull();
  });

  it("offers every configured provider, with its own label", async () => {
    options = { ...options, oauth: [{ id: "google", label: "Google" }, { id: "oidc", label: "Company SSO" }] };
    renderAt("/login");
    expect((await screen.findByRole("link", { name: "Continue with Company SSO" })).getAttribute("href")).toBe("/api/auth/oauth/oidc/start");
    expect(screen.getByRole("link", { name: "Continue with Google" })).toBeTruthy();
  });

  it("does not offer sign-up through a provider while registration is closed", async () => {
    options = { ...options, registration_open: false };
    renderAt("/register");
    expect(await screen.findByText("Registration is currently closed.")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Continue with/ })).toBeNull();
  });

  it("still works if the options cannot be fetched (the email form is the fallback)", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => (url.startsWith("/api/auth/me") ? json(401, { detail: { code: "x", message: "x" } }) : json(500, { detail: "boom" }))));
    renderAt("/login");
    expect(await screen.findByLabelText("Email")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Continue with/ })).toBeNull();
  });
});

describe("coming back from the provider with a problem", () => {
  it("explains a cancelled sign-in, then tidies the address bar", async () => {
    renderAt("/login?error=oauth_denied");
    expect(await screen.findByText(/Sign-in was cancelled/)).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/login"));
  });

  it("explains an unverified email at the provider", async () => {
    renderAt("/login?error=oauth_email_unverified");
    expect(await screen.findByText(/has not been verified by the provider/)).toBeTruthy();
  });

  it("shows nothing for a code it does not know, and does not echo it", async () => {
    renderAt("/login?error=%3Cscript%3Ealert(1)%3C/script%3E");
    await screen.findByLabelText("Email");
    expect(document.body.textContent).not.toContain("script");
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("the landing page after a successful provider sign-in", () => {
  it("trades the cookie for a session and goes to the app", async () => {
    renderAt("/oauth/complete");
    expect(screen.getByText("Signing you in…")).toBeTruthy();
    expect(await screen.findByText("home")).toBeTruthy();
    expect(calls).toContain("/api/auth/refresh");
    expect(screen.getByTestId("status").textContent).toBe("authenticated");
  });

  it("returns to sign-in with an explanation if the session cannot be created", async () => {
    refreshResponse = () => json(401, { detail: { code: "invalid_refresh_token", message: "Missing refresh token." } });
    renderAt("/oauth/complete");
    await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/login"));
    expect(await screen.findByText(/could not sign you in with that provider/i)).toBeTruthy();
  });

  it("asks for the session exactly once", async () => {
    renderAt("/oauth/complete");
    await screen.findByText("home");
    expect(calls.filter((c) => c === "/api/auth/refresh")).toHaveLength(1);
  });
});
