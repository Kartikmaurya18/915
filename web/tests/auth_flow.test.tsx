import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../src/auth/AuthContext";
import { buildE164, formatPreview } from "../src/lib/phone";
import ForgotPassword from "../src/pages/ForgotPassword";
import { AuthPage } from "../src/pages/AuthPage";
import { AllProviders } from "./providers";

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

const CHALLENGE = {
  verification_required: true,
  verification_token: "vtok",
  pending: ["email", "phone"],
  destinations: { email: "n**@example.com", phone: "+91*******210" },
  resend_after_seconds: 0,
  code_length: 6,
  code_ttl_seconds: 600,
};
const SESSION = { access_token: "at", token_type: "bearer", expires_at: new Date(Date.now() + 900_000).toISOString(), user: { id: "u1", email: "new@example.com", role: "user" } };

type Handler = (body: Record<string, unknown>) => Response;
let handlers: Record<string, Handler>;
let calls: { path: string; body: Record<string, unknown> }[];
let verification: string[] = ["email", "phone"];

beforeEach(() => {
  localStorage.clear(); // a previous test's session hint would change how the page boots
  calls = [];
  verification = ["email", "phone"];
  handlers = {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const body = init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
      const path = url.split("?")[0]!;
      calls.push({ path, body });
      if (path === "/api/auth/me") return json(401, { detail: { code: "not_authenticated", message: "x" } });
      if (path === "/api/auth/options") return json(200, { verification: "both", channels: verification, registration_open: true, code_length: 6 });
      const h = handlers[path];
      return h ? h(body) : json(404, { detail: "unmocked " + path });
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Status() {
  const { status } = useAuth();
  return <span data-testid="status">{status}</span>;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AllProviders>
        <Status />
        <Routes>
          <Route path="/register" element={<AuthPage kind="register" />} />
          <Route path="/login" element={<AuthPage kind="login" />} />
          <Route path="/forgot" element={<ForgotPassword />} />
        </Routes>
      </AllProviders>
    </MemoryRouter>,
  );
}

const type = (label: string | RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

async function fillRegistration(phone = "98765 43210") {
  await screen.findByLabelText("Mobile number");
  type("Email", "new@example.com");
  type("Mobile number", phone);
  type(/^Password/, "correct horse battery");
}

describe("phone numbers", () => {
  it("builds E.164 from a country and a national number", () => {
    expect(buildE164("91", "98765 43210")).toBe("+919876543210");
    expect(buildE164("91", "098765-43210")).toBe("+919876543210"); // one leading trunk zero is dropped
    expect(buildE164("44", "(07911) 123456")).toBe("+447911123456");
    expect(buildE164("1", "415 555 2671")).toBe("+14155552671");
  });
  it("is null while the number cannot be valid", () => {
    for (const n of ["", "123", "abc", "98765"]) expect(buildE164("91", n)).toBeNull();
    expect(buildE164("91", "9".repeat(20))).toBeNull();
  });
  it("shows the number back in a readable form", () => {
    expect(formatPreview("+919876543210", "91")).toBe("+91 98765 43210");
  });
});

describe("registration", () => {
  it("asks for a mobile number only when the server verifies phones", async () => {
    verification = ["email"];
    renderAt("/register");
    await screen.findByLabelText("Email");
    await waitFor(() => expect(calls.some((c) => c.path === "/api/auth/options")).toBe(true));
    expect(screen.queryByLabelText("Mobile number")).toBeNull();
    cleanup();
    verification = ["email", "phone"];
    renderAt("/register");
    expect(await screen.findByLabelText("Mobile number")).toBeTruthy();
  });

  it("sends the number in E.164 and shows where the code is going", async () => {
    handlers["/api/auth/register"] = () => json(202, CHALLENGE);
    renderAt("/register");
    await fillRegistration();
    expect(screen.getByText(/We will text a code to \+91 98765 43210/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Create account and send codes/ }));
    await screen.findByRole("heading", { name: "Verify your account" });
    expect(calls.find((c) => c.path === "/api/auth/register")!.body).toEqual({ email: "new@example.com", password: "correct horse battery", phone: "+919876543210" });
    expect(screen.getByText(/sent to n\*\*@example\.com/)).toBeTruthy();
    expect(screen.getByText(/sent to \+91\*+210/)).toBeTruthy();
    expect(screen.getByTestId("status").textContent).toBe("anonymous"); // nothing is signed in yet
  });

  it("refuses an invalid number before asking the server", async () => {
    renderAt("/register");
    await fillRegistration("123");
    fireEvent.click(screen.getByRole("button", { name: /Create account/ }));
    expect(await screen.findByText("Enter a valid mobile number.")).toBeTruthy();
    expect(calls.some((c) => c.path === "/api/auth/register")).toBe(false);
  });

  it("shows the server's complaint about the number under the number", async () => {
    handlers["/api/auth/register"] = () => json(422, { detail: { code: "invalid_phone", message: "Enter your mobile number with the country code." } });
    renderAt("/register");
    await fillRegistration();
    fireEvent.click(screen.getByRole("button", { name: /Create account/ }));
    expect((await screen.findByText(/with the country code/)).getAttribute("role")).toBe("alert");
  });

  it("explains an existing account", async () => {
    handlers["/api/auth/register"] = () => json(409, { detail: { code: "email_taken", message: "An account with this email already exists." } });
    renderAt("/register");
    await fillRegistration();
    fireEvent.click(screen.getByRole("button", { name: /Create account/ }));
    expect(await screen.findByText("An account with this email already exists.")).toBeTruthy();
  });

  it("still signs straight in when the server does not verify (the original behaviour)", async () => {
    verification = [];
    handlers["/api/auth/register"] = () => json(201, SESSION);
    renderAt("/register");
    await screen.findByLabelText("Email");
    type("Email", "new@example.com");
    type(/^Password/, "correct horse battery");
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("authenticated"));
  });
});

describe("entering the codes", () => {
  async function toVerify() {
    handlers["/api/auth/register"] = () => json(202, CHALLENGE);
    renderAt("/register");
    await fillRegistration();
    fireEvent.click(screen.getByRole("button", { name: /Create account/ }));
    await screen.findByRole("heading", { name: "Verify your account" });
  }
  const codeBox = (channel: "email" | "phone") => document.getElementById(`code-${channel}`) as HTMLInputElement;
  const verifyButton = (channel: "email" | "phone") => within(codeBox(channel).closest("form")!).getByRole("button", { name: "Verify" });

  it("verifies each channel on its own and signs in after the last", async () => {
    handlers["/api/auth/verify"] = (b) =>
      b.channel === "email" ? json(200, { verification_required: true, verification_token: "vtok", pending: ["phone"], verified: "email" }) : json(200, { ...SESSION, verification_required: false });
    await toVerify();
    fireEvent.change(codeBox("email"), { target: { value: "123456" } });
    fireEvent.click(verifyButton("email"));
    expect(await screen.findByText("Email verified")).toBeTruthy();
    expect(calls.find((c) => c.path === "/api/auth/verify")!.body).toEqual({ verification_token: "vtok", channel: "email", code: "123456" });
    expect(screen.getByTestId("status").textContent).toBe("anonymous");
    fireEvent.change(codeBox("phone"), { target: { value: "654321" } });
    fireEvent.click(verifyButton("phone"));
    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("authenticated"));
  });

  it("only accepts digits, caps the length, and keeps Verify disabled until the code is complete", async () => {
    await toVerify();
    fireEvent.change(codeBox("email"), { target: { value: "12ab34cd5678" } });
    expect(codeBox("email").value).toBe("123456");
    fireEvent.change(codeBox("email"), { target: { value: "123" } });
    expect((verifyButton("email") as HTMLButtonElement).disabled).toBe(true);
    expect(codeBox("email").getAttribute("autocomplete")).toBe("one-time-code");
    expect(codeBox("email").getAttribute("inputmode")).toBe("numeric");
  });

  it("shows how many attempts are left after a wrong code, and clears the box", async () => {
    handlers["/api/auth/verify"] = () => json(400, { detail: { code: "invalid_code", message: "That code is not right. 4 attempts left.", attempts_left: 4 } });
    await toVerify();
    fireEvent.change(codeBox("email"), { target: { value: "000000" } });
    fireEvent.click(verifyButton("email"));
    const alert = await screen.findByText("That code is not right. 4 attempts left.");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(codeBox("email").value).toBe("");
    expect(codeBox("email").getAttribute("aria-invalid")).toBe("true");
  });

  it("can ask for a new code, then waits out the cooldown", async () => {
    handlers["/api/auth/resend"] = () => json(202, { sent: true, resend_after_seconds: 60 });
    await toVerify();
    const button = within(codeBox("email").closest("form")!).getByRole("button", { name: "Send a new code" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    expect(await screen.findByText(/A new code is on its way to your email address/)).toBeTruthy();
    expect(calls.find((c) => c.path === "/api/auth/resend")!.body).toEqual({ verification_token: "vtok", channel: "email" });
    const waiting = await within(codeBox("email").closest("form")!).findByRole("button", { name: /Send a new code in \d+s/ });
    expect((waiting as HTMLButtonElement).disabled).toBe(true);
  });

  it("starts the countdown from the server's cooldown when a resend is refused", async () => {
    handlers["/api/auth/resend"] = () => json(429, { detail: { code: "otp_cooldown", message: "Please wait 42 seconds before asking for another code.", retry_after: 42 } }, { "Retry-After": "42" });
    await toVerify();
    fireEvent.click(within(codeBox("phone").closest("form")!).getByRole("button", { name: "Send a new code" }));
    expect(await screen.findByText(/Please wait 42 seconds/)).toBeTruthy();
    expect(await within(codeBox("phone").closest("form")!).findByRole("button", { name: /Send a new code in (41|42)s/ })).toBeTruthy();
  });

  it("returns to sign-in with an explanation when the verification session has expired", async () => {
    handlers["/api/auth/verify"] = () => json(401, { detail: { code: "verification_expired", message: "expired" } });
    await toVerify();
    fireEvent.change(codeBox("email"), { target: { value: "123456" } });
    fireEvent.click(verifyButton("email"));
    expect(await screen.findByText(/verification session ended/)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Create account" })).toBeTruthy();
  });

  it("lets the person back out to fix a mistyped number", async () => {
    await toVerify();
    fireEvent.click(screen.getByRole("button", { name: "Use a different email or number" }));
    expect(await screen.findByRole("heading", { name: "Create account" })).toBeTruthy();
  });
});

describe("signing in before verifying", () => {
  it("lands on the verification screen, not on an error", async () => {
    handlers["/api/auth/login"] = () => json(403, { detail: { code: "verification_required", message: "Verify your email and mobile number to continue.", ...CHALLENGE } });
    renderAt("/login");
    await screen.findByLabelText("Email");
    type("Email", "new@example.com");
    type("Password", "correct horse battery");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Verify your account" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("still reports a wrong password plainly", async () => {
    handlers["/api/auth/login"] = () => json(401, { detail: { code: "invalid_credentials", message: "Incorrect email or password." } });
    renderAt("/login");
    await screen.findByLabelText("Email");
    type("Email", "a@example.com");
    type("Password", "wrong wrong wrong");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Incorrect email or password.")).toBeTruthy();
  });

  it("links to password reset", async () => {
    renderAt("/login");
    expect((await screen.findByRole("link", { name: "Forgot your password?" })).getAttribute("href")).toBe("/forgot");
  });
});

describe("forgot password", () => {
  it("asks for a code, then sets a new password", async () => {
    handlers["/api/auth/password/forgot"] = () => json(202, { sent: true, resend_after_seconds: 60 });
    handlers["/api/auth/password/reset"] = () => json(200, { reset: true });
    renderAt("/forgot");
    type("Email", "new@example.com");
    fireEvent.click(screen.getByRole("radio", { name: "My mobile" }));
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/If that account exists, a code is on its way to its verified mobile number/)).toBeTruthy();
    expect(calls.find((c) => c.path === "/api/auth/password/forgot")!.body).toEqual({ email: "new@example.com", channel: "phone" });
    type("Code", "123456");
    type("New password", "a different password");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("heading", { name: "Password changed" })).toBeTruthy();
    expect(calls.find((c) => c.path === "/api/auth/password/reset")!.body).toEqual({ email: "new@example.com", channel: "phone", code: "123456", new_password: "a different password" });
    expect(screen.getByRole("link", { name: "Go to sign in" }).getAttribute("href")).toBe("/login");
  });

  it("shows a wrong-code error and keeps the form", async () => {
    handlers["/api/auth/password/forgot"] = () => json(202, { sent: true });
    handlers["/api/auth/password/reset"] = () => json(400, { detail: { code: "invalid_code", message: "That code is not right or has expired. Ask for a new one." } });
    renderAt("/forgot");
    type("Email", "new@example.com");
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    await screen.findByLabelText("Code");
    type("Code", "000000");
    type("New password", "a different password");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText(/not right or has expired/)).toBeTruthy();
    expect(screen.getByLabelText("New password")).toBeTruthy();
  });

  it("rejects a short new password before asking the server", async () => {
    handlers["/api/auth/password/forgot"] = () => json(202, { sent: true });
    renderAt("/forgot");
    type("Email", "new@example.com");
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    await screen.findByLabelText("Code");
    type("Code", "123456");
    type("New password", "short");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText(/at least 10 characters/)).toBeTruthy();
    expect(calls.some((c) => c.path === "/api/auth/password/reset")).toBe(false);
  });
});
