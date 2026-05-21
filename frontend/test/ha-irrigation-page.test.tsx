import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithQuery } from "./render";
import type {
  HaIrrigationDiscoveryResult,
  HaIrrigationRegisterBody,
  HaIrrigationRegisterResult,
  HaRegistryResponse,
} from "@/lib/types";

/**
 * Smoke test for the HA-Irrigation-Strategy admin page (P1).
 *
 * The page wires three TanStack Query calls — discovery (GET),
 * HA registry (GET), and register (mutation POST) — so the test
 * mocks all three and walks:
 *
 *   * discovery loads + the form mounts with the suggested values
 *     pre-populated and the warning banner present;
 *   * editing a draft field (room name) reflects locally;
 *   * the "Register with OCS" button fires `registerHaIrrigation()`
 *     with the camelCase body the backend agent built to.
 */

const discoverHaIrrigation = vi.fn();
const registerHaIrrigation = vi.fn();
const getHaRegistry = vi.fn();

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
    discoverHaIrrigation: (...args: unknown[]) =>
      discoverHaIrrigation(...args),
    registerHaIrrigation: (...args: unknown[]) =>
      registerHaIrrigation(...args),
    getHaRegistry: (...args: unknown[]) => getHaRegistry(...args),
  },
}));

import HaIrrigationIntegrationPage from "@/app/admin/integrations/ha-irrigation/page";

function discoveryFixture(): HaIrrigationDiscoveryResult {
  return {
    ok: true,
    suggestedRoom: { name: "F1", externalSystemId: "ha-irrigation:default" },
    detectedZoneCount: 2,
    zones: [
      {
        zoneIndex: 1,
        suggestedLocationLabel: "Zone 1",
        candidateEntities: {
          vwcSensors: ["sensor.crop_steering_vwc_zone_1"],
          ecSensors: ["sensor.crop_steering_ec_zone_1"],
          valves: ["switch.crop_steering_zone_1_valve"],
        },
      },
      {
        zoneIndex: 2,
        suggestedLocationLabel: "Zone 2",
        candidateEntities: {
          vwcSensors: ["sensor.crop_steering_vwc_zone_2"],
          ecSensors: ["sensor.crop_steering_ec_zone_2"],
          valves: ["switch.crop_steering_zone_2_valve"],
        },
      },
    ],
    roomLevelCandidates: {
      pump: ["switch.irrigation_pump"],
      mainlineValve: ["switch.irrigation_main"],
      steeringIntent: ["number.crop_steering_steering_intent"],
      ecTargets: [
        "number.crop_steering_ec_target_veg_p1",
        "number.crop_steering_ec_target_veg_p2",
      ],
      anomalyBinarySensor: ["binary_sensor.crop_steering_anomaly_active"],
      phaseSelect: ["select.crop_steering_irrigation_phase"],
      rootsenseReportSensor: [
        "sensor.crop_steering_rootsense_report_latest",
      ],
    },
    warnings: ["Zone 2 valve has no matching VWC pair — review."],
  };
}

function registerResultFixture(): HaIrrigationRegisterResult {
  return {
    ok: true,
    roomId: "room-f1",
    buildingId: "bld-1",
    locationsCreated: 2,
    locationsUpdated: 0,
    sensorsCreated: 4,
    sensorsUpdated: 0,
    equipmentCreated: 4,
    equipmentUpdated: 0,
    records: {
      roomId: "room-f1",
      buildingId: "bld-1",
      locations: [
        { zoneIndex: 1, locationId: "loc-1" },
        { zoneIndex: 2, locationId: "loc-2" },
      ],
      sensors: [],
      equipment: [],
    },
  };
}

function emptyRegistry(): HaRegistryResponse {
  return { areas: [], entities: [] };
}

describe("HaIrrigationIntegrationPage (P1)", () => {
  beforeEach(() => {
    discoverHaIrrigation.mockReset();
    registerHaIrrigation.mockReset();
    getHaRegistry.mockReset();
  });

  it("loads discovery and renders the form with the warning banner", async () => {
    discoverHaIrrigation.mockResolvedValue(discoveryFixture());
    getHaRegistry.mockResolvedValue(emptyRegistry());

    renderWithQuery(<HaIrrigationIntegrationPage />);

    // The form mounts.
    expect(
      await screen.findByTestId("ha-irrigation-discovery-form"),
    ).toBeInTheDocument();
    // Suggested room name pre-populated.
    expect(screen.getByTestId("ha-room-name")).toHaveValue("F1");
    // Both zones rendered.
    expect(screen.getByTestId("ha-zone-row-1")).toBeInTheDocument();
    expect(screen.getByTestId("ha-zone-row-2")).toBeInTheDocument();
    // Warning banner present with the warning text.
    expect(screen.getByTestId("ha-irrigation-warnings")).toBeInTheDocument();
    expect(
      screen.getByText(/Zone 2 valve has no matching VWC pair/i),
    ).toBeInTheDocument();
  });

  it("dispatches edits to the room-name input", async () => {
    discoverHaIrrigation.mockResolvedValue(discoveryFixture());
    getHaRegistry.mockResolvedValue(emptyRegistry());
    const user = userEvent.setup();

    renderWithQuery(<HaIrrigationIntegrationPage />);

    const nameInput = await screen.findByTestId("ha-room-name");
    await user.clear(nameInput);
    await user.type(nameInput, "F2");
    expect(nameInput).toHaveValue("F2");
  });

  it("fires the register POST with the confirmed mapping", async () => {
    discoverHaIrrigation.mockResolvedValue(discoveryFixture());
    registerHaIrrigation.mockResolvedValue(registerResultFixture());
    getHaRegistry.mockResolvedValue(emptyRegistry());
    const user = userEvent.setup();

    renderWithQuery(<HaIrrigationIntegrationPage />);

    const submit = await screen.findByTestId("ha-register-submit");
    // The fixture already has a valve and a room name → button enabled.
    expect(submit).not.toBeDisabled();
    await user.click(submit);

    await waitFor(() => {
      expect(registerHaIrrigation).toHaveBeenCalledTimes(1);
    });
    const firstCall = registerHaIrrigation.mock.calls[0];
    if (!firstCall) throw new Error("expected one register call");
    const body = firstCall[0] as HaIrrigationRegisterBody;
    expect(body.room.name).toBe("F1");
    expect(body.zones).toHaveLength(2);
    expect(body.zones[0]).toMatchObject({
      zoneIndex: 1,
      locationLabel: "Zone 1",
      vwcSensors: ["sensor.crop_steering_vwc_zone_1"],
      ecSensors: ["sensor.crop_steering_ec_zone_1"],
      valves: ["switch.crop_steering_zone_1_valve"],
    });
    expect(body.roomLevel.pump).toBe("switch.irrigation_pump");
    expect(body.roomLevel.mainlineValve).toBe("switch.irrigation_main");
    expect(body.roomLevel.ecTargets).toEqual([
      "number.crop_steering_ec_target_veg_p1",
      "number.crop_steering_ec_target_veg_p2",
    ]);

    // Success card shows up after the POST resolves.
    expect(
      await screen.findByTestId("ha-register-success"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("ha-register-view-rooms-link"),
    ).toHaveAttribute("href", "/admin/rooms");
  });

  it("disables register when no zones have a valve picked", async () => {
    const d = discoveryFixture();
    // Clear every valve so the validation rule trips.
    d.zones = d.zones.map((z) => ({
      ...z,
      candidateEntities: { ...z.candidateEntities, valves: [] },
    }));
    discoverHaIrrigation.mockResolvedValue(d);
    getHaRegistry.mockResolvedValue(emptyRegistry());

    renderWithQuery(<HaIrrigationIntegrationPage />);

    expect(await screen.findByTestId("ha-register-submit")).toBeDisabled();
  });

  it("re-runs discovery via the header button when the form is clean", async () => {
    discoverHaIrrigation.mockResolvedValue(discoveryFixture());
    getHaRegistry.mockResolvedValue(emptyRegistry());
    const user = userEvent.setup();

    renderWithQuery(<HaIrrigationIntegrationPage />);

    await screen.findByTestId("ha-irrigation-discovery-form");
    expect(discoverHaIrrigation).toHaveBeenCalledTimes(1);

    await user.click(screen.getByTestId("ha-rediscover"));
    await waitFor(() => {
      expect(discoverHaIrrigation).toHaveBeenCalledTimes(2);
    });
  });

  it("warns before discarding edits on re-discovery", async () => {
    discoverHaIrrigation.mockResolvedValue(discoveryFixture());
    getHaRegistry.mockResolvedValue(emptyRegistry());
    const user = userEvent.setup();

    renderWithQuery(<HaIrrigationIntegrationPage />);

    const nameInput = await screen.findByTestId("ha-room-name");
    // Dirty the form.
    await user.clear(nameInput);
    await user.type(nameInput, "F2");

    await user.click(screen.getByTestId("ha-rediscover"));
    // The dialog opens with the discard button.
    expect(
      await screen.findByTestId("ha-rediscover-confirm"),
    ).toBeInTheDocument();
    // Only the initial discover call has run so far.
    expect(discoverHaIrrigation).toHaveBeenCalledTimes(1);

    await user.click(screen.getByTestId("ha-rediscover-confirm"));
    await waitFor(() => {
      expect(discoverHaIrrigation).toHaveBeenCalledTimes(2);
    });
  });

  it("surfaces a register error inline and keeps the form editable", async () => {
    discoverHaIrrigation.mockResolvedValue(discoveryFixture());
    registerHaIrrigation.mockRejectedValue(
      new Error("Register failed (HTTP 500)."),
    );
    getHaRegistry.mockResolvedValue(emptyRegistry());
    const user = userEvent.setup();

    renderWithQuery(<HaIrrigationIntegrationPage />);

    await user.click(await screen.findByTestId("ha-register-submit"));
    expect(
      await screen.findByTestId("ha-register-error"),
    ).toBeInTheDocument();
    // Form is still mounted (no success card replaced it).
    expect(
      screen.getByTestId("ha-irrigation-discovery-form"),
    ).toBeInTheDocument();
  });
});
