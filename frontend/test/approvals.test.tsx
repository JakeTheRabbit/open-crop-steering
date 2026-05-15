import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithQuery } from "./render";
import type { ApprovalsResponse, PendingApprovalSummary } from "@/lib/types";

// Mock the api-client so the page exercises the approve / reject flow
// against controllable fakes.
const listApprovals = vi.fn();
const approveApproval = vi.fn();
const rejectApproval = vi.fn();

vi.mock("@/lib/api-client", () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: unknown;
    constructor(status: number, detail: unknown) {
      super(`API error ${status}`);
      this.status = status;
      this.detail = detail;
    }
  },
  api: {
    listApprovals: (...args: unknown[]) => listApprovals(...args),
    approveApproval: (...args: unknown[]) => approveApproval(...args),
    rejectApproval: (...args: unknown[]) => rejectApproval(...args),
  },
}));

import ApprovalsPage from "@/app/approvals/page";

function pending(
  overrides: Partial<PendingApprovalSummary> = {},
): PendingApprovalSummary {
  return {
    id: 42,
    room_id: "F2",
    status: "open",
    summary: "Lower temp_day by 0.2C — VPD drift.",
    snapshot_id: 100,
    llm_call_id: 7,
    created_at: "2026-05-16T08:00:00Z",
    expires_at: "2026-05-16T09:30:00Z",
    decided_at: null,
    decided_by: null,
    decision_channel: null,
    ...overrides,
  };
}

describe("ApprovalsPage", () => {
  beforeEach(() => {
    listApprovals.mockReset();
    approveApproval.mockReset();
    rejectApproval.mockReset();
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders open approvals from the API", async () => {
    const response: ApprovalsResponse = {
      total: 1,
      approvals: [pending()],
    };
    listApprovals.mockResolvedValue(response);

    renderWithQuery(<ApprovalsPage />);

    expect(
      await screen.findByText("Lower temp_day by 0.2C — VPD drift."),
    ).toBeInTheDocument();
    expect(screen.getByText("F2")).toBeInTheDocument();
  });

  it("shows an empty state when there are no open approvals", async () => {
    listApprovals.mockResolvedValue({ total: 0, approvals: [] });
    renderWithQuery(<ApprovalsPage />);
    expect(
      await screen.findByText(/No open approvals/i),
    ).toBeInTheDocument();
  });

  it("approves a proposal through the confirm dialog", async () => {
    listApprovals.mockResolvedValue({ total: 1, approvals: [pending()] });
    approveApproval.mockResolvedValue(
      pending({ status: "approved", decided_by: "u1" }),
    );
    const user = userEvent.setup();

    renderWithQuery(<ApprovalsPage />);
    await screen.findByTestId("approval-card");

    await user.click(screen.getByRole("button", { name: /^Approve$/i }));
    // The confirm dialog appears.
    expect(
      await screen.findByText(/Approve proposal #42/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /Confirm approve/i }),
    );

    await waitFor(() => {
      expect(approveApproval).toHaveBeenCalledWith(42, undefined);
    });
  });

  it("passes a decision note through to the reject call", async () => {
    listApprovals.mockResolvedValue({ total: 1, approvals: [pending()] });
    rejectApproval.mockResolvedValue(pending({ status: "rejected" }));
    const user = userEvent.setup();

    renderWithQuery(<ApprovalsPage />);
    await screen.findByTestId("approval-card");

    await user.click(screen.getByRole("button", { name: /^Reject$/i }));
    await screen.findByText(/Reject proposal #42/i);

    await user.type(
      screen.getByPlaceholderText(/Recorded with the decision/i),
      "not warranted",
    );
    await user.click(
      screen.getByRole("button", { name: /Confirm reject/i }),
    );

    await waitFor(() => {
      expect(rejectApproval).toHaveBeenCalledWith(42, "not warranted");
    });
  });

  it("surfaces a decision error inside the dialog", async () => {
    listApprovals.mockResolvedValue({ total: 1, approvals: [pending()] });
    approveApproval.mockRejectedValue(new Error("re-check failed"));
    const user = userEvent.setup();

    renderWithQuery(<ApprovalsPage />);
    await screen.findByTestId("approval-card");

    await user.click(screen.getByRole("button", { name: /^Approve$/i }));
    await screen.findByText(/Approve proposal #42/i);
    await user.click(
      screen.getByRole("button", { name: /Confirm approve/i }),
    );

    expect(await screen.findByText(/re-check failed/i)).toBeInTheDocument();
  });
});
