import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, request, setAccessToken, setRefreshHandler } from "../src/api/http";

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  setAccessToken("old");
});
afterEach(() => {
  setRefreshHandler(null);
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe("request", () => {
  it("sends the bearer token and parses JSON", async () => {
    fetchMock.mockResolvedValue(json(200, { ok: 1 }));
    await expect(request("/api/x")).resolves.toEqual({ ok: 1 });
    expect((fetchMock.mock.calls[0]![1] as RequestInit).headers).toMatchObject({ Authorization: "Bearer old" });
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    setRefreshHandler(async () => (setAccessToken("new"), true));
    fetchMock
      .mockResolvedValueOnce(json(401, { detail: { code: "token_expired", message: "expired" } }))
      .mockResolvedValueOnce(json(200, { ok: 1 }));
    await expect(request("/api/x")).resolves.toEqual({ ok: 1 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect((fetchMock.mock.calls[1]![1] as RequestInit).headers).toMatchObject({ Authorization: "Bearer new" });
  });

  it("shares one refresh between simultaneous 401s", async () => {
    const refresh = vi.fn(async () => (setAccessToken("new"), true));
    setRefreshHandler(refresh);
    fetchMock.mockImplementation(async (_url: string, init: RequestInit) =>
      (init.headers as Record<string, string>).Authorization === "Bearer new"
        ? json(200, {})
        : json(401, { detail: { code: "token_expired", message: "" } }),
    );
    await Promise.all([request("/a"), request("/b"), request("/c")]);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("does not loop when the refresh fails", async () => {
    setRefreshHandler(async () => false);
    fetchMock.mockResolvedValue(json(401, { detail: { code: "token_expired", message: "expired" } }));
    await expect(request("/api/x")).rejects.toMatchObject({ status: 401, code: "token_expired" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry twice when the retry is rejected as well", async () => {
    setRefreshHandler(async () => true);
    fetchMock.mockResolvedValue(json(401, { detail: { code: "invalid_token", message: "no" } }));
    await expect(request("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("turns structured, string and validation errors into ApiError", async () => {
    fetchMock.mockResolvedValueOnce(json(429, { detail: { code: "rate_limited", message: "slow down" } }, { "Retry-After": "300" }));
    await expect(request("/x", { noRefresh: true })).rejects.toMatchObject({ status: 429, code: "rate_limited", message: "slow down", retryAfter: 300 });
    fetchMock.mockResolvedValueOnce(json(400, { detail: "Quantity too large" }));
    await expect(request("/x")).rejects.toMatchObject({ status: 400, message: "Quantity too large" });
    fetchMock.mockResolvedValueOnce(json(422, { detail: [{ msg: "field required" }] }));
    await expect(request("/x")).rejects.toMatchObject({ status: 422, code: "validation", message: "field required" });
  });

  it("adds the CSRF header only for credentialed auth calls and handles 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await request("/api/auth/logout", { method: "POST", body: {}, credentials: true });
    expect((fetchMock.mock.calls[0]![1] as RequestInit).headers).toMatchObject({ "X-Requested-With": "fetch" });
    await request("/api/other");
    expect((fetchMock.mock.calls[1]![1] as RequestInit).headers).not.toHaveProperty("X-Requested-With");
  });
});
