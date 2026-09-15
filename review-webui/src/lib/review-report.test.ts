import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, fetchReviewReport, listSessions } from "./api";
import type { ApiAuth } from "./api";

const SESSION_KEY = "websocket:abc123";

function auth(refreshAuth: () => Promise<string | null> = async () => null): ApiAuth {
  return { token: "tok", refreshAuth };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const ARTIFACT = {
  run_id: "run-1a2b3c4d5e6f",
  session_key: SESSION_KEY,
  status: "completed",
  input_fingerprint: "fp-001",
  report_markdown: "## Code Review Report\n\nNo actionable issues found.",
  findings: [],
  verdicts: [],
  usage: { total_tokens: 1200 },
  warnings: [],
  created_at: "2026-09-15T04:00:00+00:00",
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("fetchReviewReport", () => {
  it("returns the verified artifact on success", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, { run_id: ARTIFACT.run_id, status: "completed", artifact: ARTIFACT }),
    );

    const report = await fetchReviewReport(auth(), SESSION_KEY);

    expect(report).not.toBeNull();
    expect(report?.run_id).toBe(ARTIFACT.run_id);
    expect(report?.status).toBe("completed");
    expect(report?.artifact.report_markdown).toContain("No actionable issues found.");
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      `/api/sessions/${encodeURIComponent(SESSION_KEY)}/review-report`,
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }),
    );
  });

  it("returns null on 404 so callers fall back to the transcript", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(404, { detail: "no report" }));

    const report = await fetchReviewReport(auth(), SESSION_KEY);

    expect(report).toBeNull();
  });

  it("raises ApiError on corrupted or mismatched artifact (409)", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(409, { detail: "artifact fingerprint mismatch" }),
    );

    await expect(fetchReviewReport(auth(), SESSION_KEY)).rejects.toMatchObject({
      status: 409,
    } satisfies Partial<ApiError>);
  });

  it("raises ApiError carrying the server detail on 409", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(409, { detail: "artifact session mismatch" }),
    );

    const error = await fetchReviewReport(auth(), SESSION_KEY).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toContain("artifact session mismatch");
  });

  it("retries once with a refreshed token after 401", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response("", { status: 401 }))
      .mockResolvedValueOnce(
        jsonResponse(200, { run_id: ARTIFACT.run_id, status: "completed", artifact: ARTIFACT }),
      );

    const report = await fetchReviewReport(auth(async () => "tok-2"), SESSION_KEY);

    expect(report?.artifact.run_id).toBe(ARTIFACT.run_id);
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(fetch).mock.calls[1]?.[1]).toMatchObject({
      headers: expect.objectContaining({ Authorization: "Bearer tok-2" }),
    });
  });

  it("surfaces 404 when the refreshed retry also fails", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response("", { status: 401 }))
      .mockResolvedValueOnce(jsonResponse(404, { detail: "no report" }));

    await expect(fetchReviewReport(auth(async () => "tok-2"), SESSION_KEY)).resolves.toBeNull();
  });

  it("escapes the session key in the request path", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(404, {}));

    await fetchReviewReport(auth(), "websocket:a/b?c");

    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe(
      `/api/sessions/${encodeURIComponent("websocket:a/b?c")}/review-report`,
    );
  });
});

describe("listSessions review metadata", () => {
  it("maps review metadata so a reconnect can restore phase, run id and report ref", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, {
        sessions: [
          {
            key: SESSION_KEY,
            created_at: "2026-09-15T04:00:00+00:00",
            updated_at: "2026-09-15T04:05:00+00:00",
            title: "Review app.py",
            preview: "",
            metadata: {
              review_run_id: "run-1a2b3c4d5e6f",
              review_status: "completed",
              review_phase: "done",
              review_report_ref: "review-artifacts/run-1a2b3c4d5e6f.json",
              review_target: "app.py",
            },
          },
        ],
      }),
    );

    const [session] = await listSessions(auth());

    expect(session.reviewRunId).toBe("run-1a2b3c4d5e6f");
    expect(session.reviewStatus).toBe("completed");
    expect(session.reviewPhase).toBe("done");
    expect(session.reviewReportRef).toBe("review-artifacts/run-1a2b3c4d5e6f.json");
  });

  it("leaves review fields undefined for legacy sessions without new metadata", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, {
        sessions: [
          {
            key: "cli:legacy",
            created_at: null,
            updated_at: null,
            title: "",
            preview: "",
            metadata: {},
          },
        ],
      }),
    );

    const [session] = await listSessions(auth());

    expect(session.reviewRunId).toBeUndefined();
    expect(session.reviewStatus).toBeUndefined();
    expect(session.reviewPhase).toBeUndefined();
    expect(session.reviewReportRef).toBeUndefined();
  });

  it("ignores blank review metadata values", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse(200, {
        sessions: [
          {
            key: SESSION_KEY,
            created_at: null,
            updated_at: null,
            title: "",
            preview: "",
            metadata: { review_run_id: "   ", review_phase: "", review_report_ref: "" },
          },
        ],
      }),
    );

    const [session] = await listSessions(auth());

    expect(session.reviewRunId).toBeUndefined();
    expect(session.reviewPhase).toBeUndefined();
    expect(session.reviewReportRef).toBeUndefined();
  });
});
