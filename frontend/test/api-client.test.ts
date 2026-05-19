import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, apiUrl } from "@/lib/api-client";

/**
 * api-client tests — URL construction (root-absolute, so requests never
 * resolve against the current sub-route) and error handling.
 */
describe("apiUrl", () => {
  it("builds a root-absolute path", () => {
    // Root-absolute so a sub-route page (/admin/rooms/) does not turn
    // the request into /admin/rooms/api/... — that 404s.
    expect(apiUrl("/api/audit/events")).toBe("/api/audit/events");
  });

  it("adds a leading slash to a path that lacks one", () => {
    expect(apiUrl("api/rollout")).toBe("/api/rollout");
  });

  it("appends defined query params and skips undefined / null / empty", () => {
    const url = apiUrl("/api/audit/events", {
      limit: 50,
      offset: 0,
      event_type: undefined,
      room_id: null,
      format: "",
    });
    expect(url).toBe("/api/audit/events?limit=50&offset=0");
  });

  it("encodes query values", () => {
    const url = apiUrl("/api/audit/export", { start: "2026-05-16" });
    expect(url).toBe("/api/audit/export?start=2026-05-16");
  });
});

describe("api.auditExportUrl", () => {
  it("forces format=csv and carries the date range", () => {
    expect(api.auditExportUrl("2026-05-01", "2026-05-16")).toBe(
      "/api/audit/export?format=csv&start=2026-05-01&end=2026-05-16",
    );
  });

  it("omits absent range bounds", () => {
    expect(api.auditExportUrl()).toBe("/api/audit/export?format=csv");
  });
});

describe("request error handling", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("throws an ApiError carrying the FastAPI detail on non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "forbidden" }), {
          status: 403,
        }),
      ),
    );
    await expect(api.getRollout()).rejects.toMatchObject({
      name: "ApiError",
      status: 403,
      detail: "forbidden",
    });
  });

  it("parses a JSON body on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ stages: [], rooms: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await api.getRollout();
    expect(result).toEqual({ stages: [], rooms: [] });
    // The root-absolute URL is what the browser receives.
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/rollout",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("sends a JSON body + content-type on POST mutations", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: 1 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await api.approveApproval(7, "looks good");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/approvals/7/approve",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ notes: "looks good" }),
      }),
    );
  });
});

describe("ApiError", () => {
  it("is an Error subclass with status + detail", () => {
    const err = new ApiError(409, "conflict");
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(409);
    expect(err.detail).toBe("conflict");
  });
});
