import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithQuery } from "./render";
import type {
  Building,
  BuildingsResponse,
  ConvexRoom,
  ConvexRoomsResponse,
  Equipment,
  EquipmentResponse,
  HaRegistryResponse,
  Sensor,
  SensorsResponse,
} from "@/lib/types";

/**
 * Smoke test for the pass-5b admin/rooms page.
 *
 * The page now talks to four endpoints (buildings / rooms /
 * sensors / equipment) and lazy-creates a "Facility" building on
 * first load. The test mocks every endpoint, asserts the page
 * mounts, and walks through the basic select-a-room flow so we
 * notice if any wiring breaks. Per-pick interactions are covered
 * by the sensor-role-picker unit tests separately.
 */

const listBuildings = vi.fn();
const createBuilding = vi.fn();
const listConvexRooms = vi.fn();
const createConvexRoom = vi.fn();
const deleteConvexRoom = vi.fn();
const listSensors = vi.fn();
const listEquipment = vi.fn();
const createSensor = vi.fn();
const deleteSensor = vi.fn();
const createEquipment = vi.fn();
const deleteEquipment = vi.fn();
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
    listBuildings: (...args: unknown[]) => listBuildings(...args),
    createBuilding: (...args: unknown[]) => createBuilding(...args),
    listConvexRooms: (...args: unknown[]) => listConvexRooms(...args),
    createConvexRoom: (...args: unknown[]) => createConvexRoom(...args),
    deleteConvexRoom: (...args: unknown[]) => deleteConvexRoom(...args),
    listSensors: (...args: unknown[]) => listSensors(...args),
    listEquipment: (...args: unknown[]) => listEquipment(...args),
    createSensor: (...args: unknown[]) => createSensor(...args),
    deleteSensor: (...args: unknown[]) => deleteSensor(...args),
    createEquipment: (...args: unknown[]) => createEquipment(...args),
    deleteEquipment: (...args: unknown[]) => deleteEquipment(...args),
    getHaRegistry: (...args: unknown[]) => getHaRegistry(...args),
  },
}));

import AdminRoomsPage from "@/app/admin/rooms/page";

function building(overrides: Partial<Building> = {}): Building {
  return {
    id: "bld-1",
    orgId: "open-crop-steering",
    name: "Facility",
    address: null,
    stories: null,
    width: null,
    height: null,
    length: null,
    createdAt: 0,
    updatedAt: 0,
    ...overrides,
  };
}

function room(overrides: Partial<ConvexRoom> = {}): ConvexRoom {
  return {
    id: "room-f2",
    orgId: "open-crop-steering",
    buildingId: "bld-1",
    name: "F2",
    purpose: null,
    story: null,
    positionX: null,
    positionY: null,
    width: null,
    height: null,
    length: null,
    area: null,
    type: null,
    createdAt: 0,
    updatedAt: 0,
    ...overrides,
  };
}

function sensor(overrides: Partial<Sensor> = {}): Sensor {
  return {
    id: "sensor-1",
    orgId: "open-crop-steering",
    name: "air temperature (sensor.f2_temp)",
    code: "sensor-f2-temp",
    type: "temperature",
    roomId: "room-f2",
    locationId: null,
    batchId: null,
    manufacturer: null,
    model: null,
    serialNumber: null,
    dataUnit: "°C",
    minValue: null,
    maxValue: null,
    accuracy: null,
    resolution: null,
    integrationId: "int-1",
    externalId: "sensor.f2_temp",
    pollInterval: null,
    status: "active",
    lastReadingTime: null,
    lastReadingValue: null,
    batteryLevel: null,
    signalStrength: null,
    lastCalibration: null,
    nextCalibration: null,
    calibrationOffset: null,
    calibrationNotes: null,
    alerts: null,
    notes: "air temperature",
    tags: null,
    isActive: true,
    createdAt: 0,
    updatedAt: 0,
    ...overrides,
  };
}

function emptyEquipmentResponse(): EquipmentResponse {
  return { equipment: [] as Equipment[] };
}

function emptySensorsResponse(): SensorsResponse {
  return { sensors: [] };
}

function emptyRegistry(): HaRegistryResponse {
  return { areas: [], entities: [] };
}

describe("AdminRoomsPage (pass 5b)", () => {
  beforeEach(() => {
    listBuildings.mockReset();
    createBuilding.mockReset();
    listConvexRooms.mockReset();
    createConvexRoom.mockReset();
    deleteConvexRoom.mockReset();
    listSensors.mockReset();
    listEquipment.mockReset();
    createSensor.mockReset();
    deleteSensor.mockReset();
    createEquipment.mockReset();
    deleteEquipment.mockReset();
    getHaRegistry.mockReset();
  });

  it("lists rooms returned by the convex rooms endpoint", async () => {
    listBuildings.mockResolvedValue({
      buildings: [building()],
    } satisfies BuildingsResponse);
    listConvexRooms.mockResolvedValue({
      rooms: [room({ id: "room-f1", name: "F1" }), room()],
    } satisfies ConvexRoomsResponse);
    getHaRegistry.mockResolvedValue(emptyRegistry());

    renderWithQuery(<AdminRoomsPage />);

    expect(await screen.findByText("F1")).toBeInTheDocument();
    expect(screen.getByText("F2")).toBeInTheDocument();
  });

  it("auto-creates the Facility building when none exist", async () => {
    listBuildings.mockResolvedValueOnce({
      buildings: [],
    } satisfies BuildingsResponse);
    createBuilding.mockResolvedValue(building());
    listConvexRooms.mockResolvedValue({
      rooms: [],
    } satisfies ConvexRoomsResponse);
    getHaRegistry.mockResolvedValue(emptyRegistry());

    renderWithQuery(<AdminRoomsPage />);

    await waitFor(() => {
      expect(createBuilding).toHaveBeenCalledWith({ name: "Facility" });
    });
  });

  it("loads sensors and equipment for the selected room", async () => {
    listBuildings.mockResolvedValue({
      buildings: [building()],
    } satisfies BuildingsResponse);
    listConvexRooms.mockResolvedValue({
      rooms: [room()],
    } satisfies ConvexRoomsResponse);
    listSensors.mockResolvedValue({
      sensors: [sensor()],
    } satisfies SensorsResponse);
    listEquipment.mockResolvedValue(emptyEquipmentResponse());
    getHaRegistry.mockResolvedValue(emptyRegistry());

    const user = userEvent.setup();
    renderWithQuery(<AdminRoomsPage />);

    // The list mounts; click the room to select it.
    const item = await screen.findByTestId("room-list-item");
    await user.click(item);

    // The editor renders and the sensors/equipment queries fire.
    await waitFor(() => {
      expect(listSensors).toHaveBeenCalledWith(
        { roomId: "room-f2" },
        expect.anything(),
      );
      expect(listEquipment).toHaveBeenCalledWith(
        { roomId: "room-f2" },
        expect.anything(),
      );
    });
    expect(
      await screen.findByTestId("room-equipment-form"),
    ).toBeInTheDocument();
  });

  it("shows an empty-state when the facility has no rooms", async () => {
    listBuildings.mockResolvedValue({
      buildings: [building()],
    } satisfies BuildingsResponse);
    listConvexRooms.mockResolvedValue({
      rooms: [],
    } satisfies ConvexRoomsResponse);
    getHaRegistry.mockResolvedValue(emptyRegistry());

    renderWithQuery(<AdminRoomsPage />);
    expect(
      await screen.findByText(/No rooms in this facility/i),
    ).toBeInTheDocument();
  });

  it("surfaces a registry-load error in a banner", async () => {
    listBuildings.mockResolvedValue({
      buildings: [building()],
    } satisfies BuildingsResponse);
    listConvexRooms.mockResolvedValue({
      rooms: [],
    } satisfies ConvexRoomsResponse);
    getHaRegistry.mockRejectedValue(new Error("HA registry fetch failed"));

    renderWithQuery(<AdminRoomsPage />);
    expect(
      await screen.findByText(
        /Could not load the Home Assistant entity registry/i,
      ),
    ).toBeInTheDocument();
  });
});
