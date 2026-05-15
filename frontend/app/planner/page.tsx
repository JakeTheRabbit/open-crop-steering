"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { PageHeader } from "@/components/page-header";
import { QueryState } from "@/components/query-state";
import { Card, CardContent } from "@/components/ui/card";
import { RoomStateBadge } from "@/components/status-badges";

/**
 * Recipe-planner room picker.
 *
 * The planner is per-room (`/planner/[room]`); this index lists the
 * configured rooms so the operator can pick one.
 */
export default function PlannerIndexPage() {
  const rolloutQuery = useQuery({
    queryKey: queryKeys.rollout,
    queryFn: ({ signal }) => api.getRollout(signal),
  });
  const rooms = rolloutQuery.data?.rooms ?? [];

  return (
    <div>
      <PageHeader
        title="Recipe Planner"
        description="Choose a room to view and edit its cultivation recipe."
      />
      <QueryState
        isLoading={rolloutQuery.isLoading}
        isError={rolloutQuery.isError}
        error={rolloutQuery.error}
        isEmpty={rooms.length === 0}
        emptyMessage="No rooms configured yet."
      >
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {rooms.map((r) => (
            <Link
              key={r.room_id}
              href={`/planner/${encodeURIComponent(r.room_id)}`}
              className="block"
            >
              <Card className="transition-colors hover:border-primary/60">
                <CardContent className="flex items-center justify-between gap-2 p-4">
                  <div>
                    <div className="font-mono text-sm font-semibold uppercase">
                      {r.room_id}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      Stage: {r.current_stage.name}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <RoomStateBadge state={r.current_state} />
                    <ArrowUpRight className="h-4 w-4 text-muted-foreground" />
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      </QueryState>
    </div>
  );
}
