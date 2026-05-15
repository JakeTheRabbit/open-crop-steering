import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  RoomCard,
  type RoomCardData,
} from "@/components/dashboard/room-card";
import type { RolloutRoom, RoomState } from "@/lib/types";

/** Build a rollout-room fixture with a given health state. */
function rolloutRoom(
  state: RoomState,
  overrides: Partial<RolloutRoom> = {},
): RolloutRoom {
  return {
    room_id: "F1",
    can_advance: false,
    current_stage: { index: 0, name: "report_only", description: "" },
    next_stage: null,
    no_open_deviations: true,
    open_deviation_count: 0,
    sfw_rejections_7d: 0,
    sfw_rejection_gate_met: true,
    min_days_at_stage_met: true,
    qap_approval_recorded: false,
    blockers: [],
    paused: false,
    muted: false,
    current_state: state,
    ...overrides,
  };
}

describe("RoomCard", () => {
  it("renders the room id and a HEALTHY state badge", () => {
    const data: RoomCardData = { rollout: rolloutRoom("healthy") };
    render(<RoomCard data={data} />);
    expect(screen.getByText("F1")).toBeInTheDocument();
    expect(screen.getByText("HEALTHY")).toBeInTheDocument();
  });

  it("shows CRITICAL state for a critical room", () => {
    render(<RoomCard data={{ rollout: rolloutRoom("critical") }} />);
    expect(screen.getByText("CRITICAL")).toBeInTheDocument();
  });

  it("shows IMPAIRED state for an impaired room", () => {
    render(<RoomCard data={{ rollout: rolloutRoom("impaired") }} />);
    expect(screen.getByText("IMPAIRED")).toBeInTheDocument();
  });

  it("renders the rollout stage name", () => {
    render(<RoomCard data={{ rollout: rolloutRoom("healthy") }} />);
    expect(screen.getByText("report_only")).toBeInTheDocument();
  });

  it("surfaces the open formal-deviation count", () => {
    const data: RoomCardData = {
      rollout: rolloutRoom("critical", { open_deviation_count: 3 }),
    };
    render(<RoomCard data={data} />);
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("deviations")).toBeInTheDocument();
  });

  it("shows overlay count and recipe version when provided", () => {
    const data: RoomCardData = {
      rollout: rolloutRoom("healthy"),
      activeOverlays: 2,
      recipeVersion: 12,
    };
    render(<RoomCard data={data} />);
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("v12")).toBeInTheDocument();
  });

  it("falls back gracefully when no snapshot params are available", () => {
    render(<RoomCard data={{ rollout: rolloutRoom("healthy") }} />);
    expect(
      screen.getByText(/No recent snapshot for key parameters/i),
    ).toBeInTheDocument();
  });

  it("renders key-param readings and flags an out-of-band actual", () => {
    const data: RoomCardData = {
      rollout: rolloutRoom("healthy"),
      params: [
        {
          param_name: "temp_day",
          effective: 28,
          actual: 28.1,
          unit: "C",
          tolerance: 0.5,
        },
        {
          param_name: "rh_day",
          effective: 60,
          actual: 71,
          unit: "%",
          tolerance: 3,
        },
      ],
    };
    render(<RoomCard data={data} />);
    expect(screen.getByText("temp_day")).toBeInTheDocument();
    // The RH actual (71) is well outside the +/-3 band of 60.
    const rhActual = screen.getByText("71%");
    expect(rhActual).toHaveClass("text-impaired");
  });

  it("shows paused / muted badges when the room is paused or muted", () => {
    const data: RoomCardData = {
      rollout: rolloutRoom("healthy", { paused: true, muted: true }),
    };
    render(<RoomCard data={data} />);
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getByText("muted")).toBeInTheDocument();
  });

  it("links to the room's planner", () => {
    render(<RoomCard data={{ rollout: rolloutRoom("healthy") }} />);
    const link = screen.getByRole("link", { name: /open planner/i });
    expect(link).toHaveAttribute("href", "/planner/F1");
  });
});
