"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { Deviation } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState, errorText } from "@/components/query-state";
import { SeverityBadge } from "@/components/status-badges";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Card } from "@/components/ui/card";
import { formatTs } from "@/lib/utils";

/**
 * Formal-deviation register.
 *
 * Lists open (unacknowledged) formal deviations. A deviation blocks
 * rollout advancement for its room until a QAP acknowledges it; the ack
 * dialog collects the corrective-action note recorded in the audit
 * trail. QAP-gated on the backend for the ack action.
 */
export default function DeviationsPage() {
  const queryClient = useQueryClient();
  const [ackTarget, setAckTarget] = React.useState<Deviation | null>(null);
  const [notes, setNotes] = React.useState("");

  const deviationsQuery = useQuery({
    queryKey: queryKeys.deviations(),
    queryFn: ({ signal }) => api.getDeviations(undefined, signal),
  });

  const ackMutation = useMutation({
    mutationFn: (deviation: Deviation) =>
      api.ackDeviation(deviation.id, notes || undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["deviations"] });
      queryClient.invalidateQueries({ queryKey: queryKeys.rollout });
      closeDialog();
    },
  });

  const closeDialog = () => {
    setAckTarget(null);
    setNotes("");
    ackMutation.reset();
  };

  const deviations = deviationsQuery.data?.deviations ?? [];

  return (
    <div>
      <PageHeader
        title="Formal Deviations"
        description="Open deviations blocking rollout advancement until QAP acknowledgement."
      />

      <QueryState
        isLoading={deviationsQuery.isLoading}
        isError={deviationsQuery.isError}
        error={deviationsQuery.error}
        isEmpty={deviations.length === 0}
        emptyMessage="No open formal deviations. The facility is clear."
      >
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-44">When</TableHead>
                <TableHead className="w-24">Severity</TableHead>
                <TableHead>Room</TableHead>
                <TableHead>Summary</TableHead>
                <TableHead className="w-28 text-right">Action</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {deviations.map((d) => (
                <TableRow key={d.id} data-testid="deviation-row">
                  <TableCell className="tabular text-muted-foreground">
                    {d.id}
                  </TableCell>
                  <TableCell className="text-xs">
                    {formatTs(d.occurred_at)}
                  </TableCell>
                  <TableCell>
                    <SeverityBadge severity={d.severity} />
                  </TableCell>
                  <TableCell className="font-mono text-xs uppercase">
                    {d.room_id ?? "—"}
                  </TableCell>
                  <TableCell className="max-w-md text-xs">
                    {d.summary}
                    {d.reason_codes.length > 0 ? (
                      <span className="ml-1 inline-flex flex-wrap gap-1">
                        {d.reason_codes.map((c) => (
                          <Badge key={c} variant="outline">
                            {c}
                          </Badge>
                        ))}
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setAckTarget(d)}
                    >
                      <ShieldCheck className="h-3.5 w-3.5" />
                      Acknowledge
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      </QueryState>

      <Dialog
        open={ackTarget !== null}
        onClose={closeDialog}
        title={`Acknowledge deviation #${ackTarget?.id}`}
        description="QAP acknowledgement clears the rollout block for this room and is recorded in the audit chain."
        footer={
          <>
            <Button variant="outline" onClick={closeDialog}>
              Cancel
            </Button>
            <Button
              disabled={ackMutation.isPending}
              onClick={() => ackTarget && ackMutation.mutate(ackTarget)}
            >
              {ackMutation.isPending ? "Acknowledging…" : "Confirm ack"}
            </Button>
          </>
        }
      >
        {ackTarget ? (
          <p className="mb-3 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
            {ackTarget.summary}
          </p>
        ) : null}
        <label className="mb-1 block text-xs text-muted-foreground">
          Corrective-action note (optional)
        </label>
        <Textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="e.g. the corrective action taken — recorded in the audit trail."
        />
        {ackMutation.isError ? (
          <p className="mt-2 text-xs text-critical">
            {errorText(ackMutation.error)}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}
