import { describe, expect, it } from "vitest";

import { cn, fmtNum, formatTs, relativeAge } from "@/lib/utils";

describe("cn", () => {
  it("merges classes and resolves Tailwind conflicts (last wins)", () => {
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(cn("text-sm", false && "hidden", "font-bold")).toBe(
      "text-sm font-bold",
    );
  });
});

describe("fmtNum", () => {
  it("trims trailing zeros", () => {
    expect(fmtNum(28.5)).toBe("28.5");
    expect(fmtNum(60)).toBe("60");
    expect(fmtNum(28.123456, 2)).toBe("28.12");
  });
  it("returns a dash for non-finite input", () => {
    expect(fmtNum(Number.NaN)).toBe("—");
    expect(fmtNum(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("formatTs", () => {
  it("returns a dash for null / undefined", () => {
    expect(formatTs(null)).toBe("—");
    expect(formatTs(undefined)).toBe("—");
  });
  it("renders a parseable timestamp", () => {
    expect(formatTs("2026-05-16T08:00:00Z")).not.toBe("—");
  });
  it("passes through an unparseable string unchanged", () => {
    expect(formatTs("not-a-date")).toBe("not-a-date");
  });
});

describe("relativeAge", () => {
  it("returns a dash for null", () => {
    expect(relativeAge(null)).toBe("—");
  });
  it("renders minute-scale ages", () => {
    const fiveMinAgo = new Date(Date.now() - 5 * 60_000).toISOString();
    expect(relativeAge(fiveMinAgo)).toBe("5m");
  });
  it("renders hour-scale ages", () => {
    const threeHoursAgo = new Date(Date.now() - 3 * 3_600_000).toISOString();
    expect(relativeAge(threeHoursAgo)).toBe("3h");
  });
});
