import Link from "next/link";
import { ArrowUpRight, GitCommitVertical, Layers, ShieldAlert } from "lucide-react";

import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RoomStateBadge } from "@/components/status-badges";
import { cn, fmtNum } from "@/lib/utils";
import type { RolloutRoom } from "@/lib/types";

/**
 * One key-parameter row on a room card: effective target vs actual.
 *
 * `effective` is the recipe value plus active overlay deltas; `actual`
 * is the live reading. A reading outside the tolerance band shows in the
 * impaired colour so the operator's eye lands on it.
 */
export interface ParamReading {
  param_name: string;
  effective: number | null;
  actual: number | null;
  unit: string | null;
  tolerance: number | null;
}

export interface RoomCardData {
  rollout: RolloutRoom;
  /** Key-param readings, if a recent snapshot is available. */
  params?: ParamReading[];
  /** Count of active runtime overlays for the room. */
  activeOverlays?: number;
  /** Active recipe revision version, if known. */
  recipeVersion?: number | null;
}

function inBand(r: ParamReading): boolean {
  if (r.effective == null || r.actual == null) return true;
  const tol = r.tolerance ?? 0;
  return Math.abs(r.actual - r.effective) <= tol + 1e-9;
}

function ParamRow({ reading }: { reading: ParamReading }) {
  const ok = inBand(reading);
  const unit = reading.unit ?? "";
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5 text-xs">
      <span className="text-muted-foreground">{reading.param_name}</span>
      <span className="tabular flex items-baseline gap-1.5">
        <span className={cn(!ok && "font-semibold text-impaired")}>
          {reading.actual == null ? "—" : fmtNum(reading.actual)}
          {unit}
        </span>
        <span className="text-muted-foreground">/</span>
        <span className="text-foreground">
          {reading.effective == null ? "—" : fmtNum(reading.effective)}
          {unit}
        </span>
      </span>
    </div>
  );
}

/**
 * Dashboard per-room card.
 *
 * Shows the room health state, rollout stage, open formal-deviation
 * count, active-overlay count, recipe version, and — when a snapshot is
 * available — key effective-target-vs-actual readings. Links through to
 * the room's recipe planner.
 */
export function RoomCard({ data }: { data: RoomCardData }) {
  const { rollout, params, activeOverlays, recipeVersion } = data;
  const deviations = rollout.open_deviation_count;
  const stateBorder =
    rollout.current_state === "critical"
      ? "border-critical/50"
      : rollout.current_state === "impaired"
        ? "border-impaired/40"
        : "border-border";

  return (
    <Card className={cn("flex flex-col", stateBorder)} data-testid="room-card">
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="font-mono text-sm uppercase">
            {rollout.room_id}
          </CardTitle>
          <RoomStateBadge state={rollout.current_state} />
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="outline" title="Rollout stage">
            <GitCommitVertical className="h-3 w-3" />
            {rollout.current_stage.name}
          </Badge>
          {rollout.paused ? (
            <Badge variant="secondary">paused</Badge>
          ) : null}
          {rollout.muted ? <Badge variant="secondary">muted</Badge> : null}
        </div>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col gap-3 pt-0">
        <div className="grid grid-cols-3 gap-2 text-center">
          <div
            className={cn(
              "rounded-md border border-border bg-muted/40 px-1 py-1.5",
              deviations > 0 && "border-critical/50 bg-critical/10",
            )}
          >
            <div
              className={cn(
                "tabular text-base font-semibold",
                deviations > 0 ? "text-critical" : "text-foreground",
              )}
            >
              {deviations}
            </div>
            <div className="flex items-center justify-center gap-1 text-2xs text-muted-foreground">
              <ShieldAlert className="h-3 w-3" />
              deviations
            </div>
          </div>
          <div className="rounded-md border border-border bg-muted/40 px-1 py-1.5">
            <div className="tabular text-base font-semibold">
              {activeOverlays ?? "—"}
            </div>
            <div className="flex items-center justify-center gap-1 text-2xs text-muted-foreground">
              <Layers className="h-3 w-3" />
              overlays
            </div>
          </div>
          <div className="rounded-md border border-border bg-muted/40 px-1 py-1.5">
            <div className="tabular text-base font-semibold">
              {recipeVersion != null ? `v${recipeVersion}` : "—"}
            </div>
            <div className="text-2xs text-muted-foreground">recipe</div>
          </div>
        </div>

        {params && params.length > 0 ? (
          <div className="rounded-md border border-border px-2 py-1">
            <div className="mb-0.5 flex justify-between text-2xs uppercase tracking-wide text-muted-foreground">
              <span>param</span>
              <span>actual / target</span>
            </div>
            {params.map((p) => (
              <ParamRow key={p.param_name} reading={p} />
            ))}
          </div>
        ) : (
          <p className="rounded-md border border-dashed border-border px-2 py-3 text-center text-2xs text-muted-foreground">
            No recent snapshot for key parameters.
          </p>
        )}

        <Button
          asChild
          variant="outline"
          size="sm"
          className="mt-auto w-full"
        >
          <Link href={`/planner/${encodeURIComponent(rollout.room_id)}`}>
            Open planner
            <ArrowUpRight className="h-3.5 w-3.5" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  );
}
