import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  buildGridModel,
  cellKey,
  PlannerGrid,
} from "@/components/planner/planner-grid";
import type { RecipeParamCell } from "@/lib/types";

/** A small 2-param x 3-day recipe fixture. */
function fixtureCells(): RecipeParamCell[] {
  const cells: RecipeParamCell[] = [];
  for (let day = 1; day <= 3; day += 1) {
    cells.push({
      day_index: day,
      param_name: "temp_day",
      value: 28,
      tolerance: 0.5,
      unit: "C",
    });
    cells.push({
      day_index: day,
      param_name: "rh_day",
      value: 60,
      tolerance: 3,
      unit: "%",
    });
  }
  return cells;
}

describe("buildGridModel", () => {
  it("derives sorted param names, meta and a keyed value map", () => {
    const model = buildGridModel(fixtureCells());
    expect(model.paramNames).toEqual(["rh_day", "temp_day"]);
    expect(model.paramMeta.temp_day).toEqual({ unit: "C", tolerance: 0.5 });
    expect(model.values.get(cellKey(2, "temp_day"))).toBe(28);
    expect(model.values.size).toBe(6);
  });
});

describe("PlannerGrid", () => {
  it("renders a header column per cycle day and a row per parameter", () => {
    const model = buildGridModel(fixtureCells());
    render(
      <PlannerGrid
        paramNames={model.paramNames}
        dayCount={3}
        paramMeta={model.paramMeta}
        values={model.values}
        original={model.values}
        onEdit={() => {}}
      />,
    );
    expect(screen.getByTestId("planner-grid")).toBeInTheDocument();
    expect(screen.getByText("D1")).toBeInTheDocument();
    expect(screen.getByText("D3")).toBeInTheDocument();
    expect(screen.getByText("temp_day")).toBeInTheDocument();
    expect(screen.getByText("rh_day")).toBeInTheDocument();
  });

  it("shows the read-only tolerance band beneath each parameter", () => {
    const model = buildGridModel(fixtureCells());
    render(
      <PlannerGrid
        paramNames={model.paramNames}
        dayCount={3}
        paramMeta={model.paramMeta}
        values={model.values}
        original={model.values}
        onEdit={() => {}}
      />,
    );
    expect(screen.getByText(/±0.5/)).toBeInTheDocument();
    expect(screen.getByText(/±3/)).toBeInTheDocument();
  });

  it("fires onEdit with the parsed value when a cell is committed", async () => {
    const model = buildGridModel(fixtureCells());
    const onEdit = vi.fn();
    const user = userEvent.setup();
    render(
      <PlannerGrid
        paramNames={model.paramNames}
        dayCount={3}
        paramMeta={model.paramMeta}
        values={model.values}
        original={model.values}
        onEdit={onEdit}
      />,
    );
    const cell = screen.getByLabelText("day 1 temp_day");
    await user.clear(cell);
    await user.type(cell, "29.5");
    await user.tab(); // blur commits

    expect(onEdit).toHaveBeenCalledWith(1, "temp_day", 29.5);
  });

  it("does not fire onEdit when the value is unchanged", async () => {
    const model = buildGridModel(fixtureCells());
    const onEdit = vi.fn();
    const user = userEvent.setup();
    render(
      <PlannerGrid
        paramNames={model.paramNames}
        dayCount={3}
        paramMeta={model.paramMeta}
        values={model.values}
        original={model.values}
        onEdit={onEdit}
      />,
    );
    const cell = screen.getByLabelText("day 1 rh_day");
    await user.click(cell);
    await user.tab();
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("marks an edited cell as changed against the original", () => {
    const model = buildGridModel(fixtureCells());
    // Working copy diverges from the original at day-1 temp_day.
    const edited = new Map(model.values);
    edited.set(cellKey(1, "temp_day"), 30);
    render(
      <PlannerGrid
        paramNames={model.paramNames}
        dayCount={3}
        paramMeta={model.paramMeta}
        values={edited}
        original={model.values}
        onEdit={() => {}}
      />,
    );
    const cell = screen.getByLabelText("day 1 temp_day") as HTMLInputElement;
    expect(cell.value).toBe("30");
    expect(cell).toHaveClass("bg-accent/20");
  });

  it("renders cells read-only when readOnly is set", () => {
    const model = buildGridModel(fixtureCells());
    render(
      <PlannerGrid
        paramNames={model.paramNames}
        dayCount={3}
        paramMeta={model.paramMeta}
        values={model.values}
        original={model.values}
        onEdit={() => {}}
        readOnly
      />,
    );
    const cell = screen.getByLabelText("day 1 temp_day") as HTMLInputElement;
    expect(cell.readOnly).toBe(true);
  });
});
