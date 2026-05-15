"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
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
import { fmtNum } from "@/lib/utils";

/** Plain-language note for each parameter class. */
const CLASS_NOTES: Record<string, string> = {
  A: "Read / report only — the AI never writes Class A.",
  B: "Environment (temp, RH, CO2, VPD, PPFD, photoperiod, air velocity).",
  C: "Irrigation (shot sizes, VWC / EC targets, dryback, drain %, freq).",
  D: "Nutrient / chem (tank pH / EC, leaf temp) — tighter caps, longer cool-down.",
  E: "Admin / control state — the AI never writes Class E.",
};

function classVariant(cls: string) {
  if (cls === "D" || cls === "E") return "critical" as const;
  if (cls === "C") return "warning" as const;
  return "secondary" as const;
}

function fmtOrDash(v: number | null): string {
  return v == null ? "—" : fmtNum(v);
}

/**
 * Guardrail bounds — read-only display.
 *
 * Shows the absolute bounds, cumulative-delta caps and cool-down per
 * parameter that the Phase-9 bounded-action validator enforces.
 * Guardrail bounds are Class-E config (admin-owned, the AI never
 * writes them); editing is out of scope for this read-only view.
 */
export default function AdminGuardrailsPage() {
  const guardrailsQuery = useQuery({
    queryKey: queryKeys.guardrails,
    queryFn: () => api.getGuardrails(),
    retry: false,
  });

  const bounds = guardrailsQuery.data?.bounds ?? [];

  return (
    <div>
      <PageHeader
        title="Guardrails"
        description="Absolute bounds and cumulative caps enforced on every AI-proposed change. Read-only."
      />

      <Card className="mb-4">
        <CardContent className="p-3 text-xs text-muted-foreground">
          The bounded-action validator rejects any proposal that would move a
          parameter outside these bounds or breach a rolling cumulative cap.
          These values are Class-E configuration — admin-owned; the AI never
          writes them.
        </CardContent>
      </Card>

      <QueryState
        isLoading={guardrailsQuery.isLoading}
        isError={guardrailsQuery.isError}
        error={guardrailsQuery.error}
        isEmpty={bounds.length === 0}
        emptyMessage="No guardrail bounds configured."
        notFoundMessage="The guardrails API is not exposed on this backend build yet. Guardrail bounds still apply server-side; this page will populate once the read endpoint ships."
      >
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Parameter</TableHead>
                <TableHead className="w-20">Class</TableHead>
                <TableHead className="w-28 text-right">Min</TableHead>
                <TableHead className="w-28 text-right">Max</TableHead>
                <TableHead className="w-28 text-right">Cap 24h</TableHead>
                <TableHead className="w-28 text-right">Cap 7d</TableHead>
                <TableHead className="w-28 text-right">Cool-down</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {bounds.map((b) => (
                <TableRow key={b.param_name} data-testid="guardrail-row">
                  <TableCell className="text-sm font-medium">
                    {b.param_name}
                    {b.unit ? (
                      <span className="ml-1 text-2xs text-muted-foreground">
                        {b.unit}
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={classVariant(b.param_class)}
                      title={CLASS_NOTES[b.param_class]}
                    >
                      Class {b.param_class}
                    </Badge>
                  </TableCell>
                  <TableCell className="tabular text-right text-sm">
                    {fmtOrDash(b.min_value)}
                  </TableCell>
                  <TableCell className="tabular text-right text-sm">
                    {fmtOrDash(b.max_value)}
                  </TableCell>
                  <TableCell className="tabular text-right text-sm">
                    {fmtOrDash(b.max_cumulative_delta_24h)}
                  </TableCell>
                  <TableCell className="tabular text-right text-sm">
                    {fmtOrDash(b.max_cumulative_delta_7d)}
                  </TableCell>
                  <TableCell className="tabular text-right text-sm">
                    {b.cooldown_minutes == null
                      ? "—"
                      : `${b.cooldown_minutes} min`}
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
