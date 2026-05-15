"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckSquare, ShieldAlert } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { PageHeader } from "@/components/page-header";
import { QueryState } from "@/components/query-state";
import { RoomCard, type RoomCardData } from "@/components/dashboard/room-card";
import { Card, CardContent } from "@/components/ui/card";
import type { RolloutRoom } from "@/lib/types";

/** A single headline metric tile. */
function StatTile({
  label,
  value,
  icon,
  tone = "default",
}: {
  label: string;
  value: number | string;
  icon: React.ReactNode;
  tone?: "default" | "critical";
}) {
  return (
    <Card>
      <CardContent className="flex items-center gap-3 p-4">
        <div
          className={
            tone === "critical"
              ? "rounded-md bg-critical/15 p-2 text-critical"
              : "rounded-md bg-primary/15 p-2 text-primary"
          }
        >
          {icon}
        </div>
        <div>
          <div className="tabular text-xl font-semibold leading-none">
            {value}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">{label}</div>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * Dashboard — facility overview.
 *
 * Per-room cards plus headline counts. The room state, rollout stage and
 * open-deviation count come from `/api/rollout`; per-parameter readings,
 * overlay counts and recipe version are enrichment shown when a snapshot
 * surface is available (Phase-10 ships the rollout-driven card; the
 * planner page exposes the full per-room recipe detail).
 */
export default function DashboardPage() {
  const rolloutQuery = useQuery({
    queryKey: queryKeys.rollout,
    queryFn: ({ signal }) => api.getRollout(signal),
  });
  const approvalsQuery = useQuery({
    queryKey: queryKeys.approvals(false),
    queryFn: () => api.listApprovals(false),
  });

  const rooms: RolloutRoom[] = useMemo(
    () => rolloutQuery.data?.rooms ?? [],
    [rolloutQuery.data],
  );
  const cards: RoomCardData[] = useMemo(
    () => rooms.map((r) => ({ rollout: r })),
    [rooms],
  );

  const totalDeviations = rooms.reduce(
    (sum, r) => sum + r.open_deviation_count,
    0,
  );
  const openApprovals = approvalsQuery.data?.total ?? 0;
  const impaired = rooms.filter(
    (r) => r.current_state !== "healthy",
  ).length;

  return (
    <div>
      <PageHeader
        title="Dashboard"
        description="Per-room cultivation state across the facility."
      />

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatTile
          label="Rooms"
          value={rooms.length}
          icon={<ShieldAlert className="h-5 w-5" />}
        />
        <StatTile
          label="Rooms not healthy"
          value={impaired}
          tone={impaired > 0 ? "critical" : "default"}
          icon={<ShieldAlert className="h-5 w-5" />}
        />
        <StatTile
          label="Open deviations"
          value={totalDeviations}
          tone={totalDeviations > 0 ? "critical" : "default"}
          icon={<ShieldAlert className="h-5 w-5" />}
        />
        <StatTile
          label="Pending approvals"
          value={openApprovals}
          icon={<CheckSquare className="h-5 w-5" />}
        />
      </div>

      <QueryState
        isLoading={rolloutQuery.isLoading}
        isError={rolloutQuery.isError}
        error={rolloutQuery.error}
        isEmpty={cards.length === 0}
        emptyMessage="No rooms configured yet. Add a room in the add-on configuration."
      >
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {cards.map((card) => (
            <RoomCard key={card.rollout.room_id} data={card} />
          ))}
        </div>
      </QueryState>
    </div>
  );
}
