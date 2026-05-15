"use client";

import { useQuery } from "@tanstack/react-query";
import { Clock } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { NoTouchWindow } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Card, CardContent } from "@/components/ui/card";

const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Render a window's weekday restriction. */
function weekdayLabel(weekdays: number[]): string {
  if (!weekdays || weekdays.length === 0) return "Every day";
  return [...weekdays]
    .sort((a, b) => a - b)
    .map((d) => WEEKDAY_NAMES[d] ?? `?${d}`)
    .join(", ");
}

/** A window wraps past midnight when end <= start. */
function wrapsMidnight(w: NoTouchWindow): boolean {
  return w.end <= w.start;
}

/**
 * No-touch windows — read-only display.
 *
 * Recurring daily intervals during which the supervisor leaves a room
 * alone (no proposals, no bounded auto-adjust). Windows are Class-E
 * configuration; this view is read-only.
 */
export default function AdminNoTouchWindowsPage() {
  const windowsQuery = useQuery({
    queryKey: queryKeys.noTouchWindows,
    queryFn: () => api.getNoTouchWindows(),
    retry: false,
  });

  const windows = windowsQuery.data?.windows ?? [];

  return (
    <div>
      <PageHeader
        title="No-Touch Windows"
        description="Recurring intervals where the AI must not act. Read-only."
      />

      <Card className="mb-4">
        <CardContent className="p-3 text-xs text-muted-foreground">
          During a no-touch window the supervisor skips the room entirely — no
          proposals and no bounded auto-adjust. Supervised approvals are still
          possible. Typical use: the lights-on / lights-off transition, when
          the room is in flux. These are Class-E configuration.
        </CardContent>
      </Card>

      <QueryState
        isLoading={windowsQuery.isLoading}
        isError={windowsQuery.isError}
        error={windowsQuery.error}
        isEmpty={windows.length === 0}
        emptyMessage="No no-touch windows configured."
        notFoundMessage="The no-touch-windows API is not exposed on this backend build yet. Configured windows still apply server-side; this page will populate once the read endpoint ships."
      >
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Label</TableHead>
                <TableHead className="w-32">Start</TableHead>
                <TableHead className="w-32">End</TableHead>
                <TableHead>Days</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {windows.map((w, i) => (
                <TableRow key={`${w.label}-${w.start}-${i}`}>
                  <TableCell className="text-sm font-medium">
                    <span className="flex items-center gap-1.5">
                      <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                      {w.label || "Unnamed window"}
                    </span>
                  </TableCell>
                  <TableCell className="tabular text-sm">{w.start}</TableCell>
                  <TableCell className="tabular text-sm">
                    {w.end}
                    {wrapsMidnight(w) ? (
                      <Badge variant="secondary" className="ml-2">
                        +1 day
                      </Badge>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {weekdayLabel(w.weekdays)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      </QueryState>
    </div>
  );
}
