"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, X } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { PendingApprovalSummary } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState, errorText } from "@/components/query-state";
import { PendingStatusBadge } from "@/components/status-badges";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatTs, relativeAge } from "@/lib/utils";

type Decision = "approve" | "reject";

interface DecisionTarget {
  pending: PendingApprovalSummary;
  decision: Decision;
}

/**
 * SFW approvals — the UI side of the supervised-approval workflow.
 *
 * Lists open pending AI proposals; each can be approved or rejected
 * (cultivator+ on the backend). A confirm dialog collects an optional
 * decision note. On approve the backend re-checks the proposal before
 * applying it as runtime overlays; either decision produces one audit
 * row.
 */
export default function ApprovalsPage() {
  const queryClient = useQueryClient();
  const [includeDecided, setIncludeDecided] = React.useState(false);
  const [target, setTarget] = React.useState<DecisionTarget | null>(null);
  const [notes, setNotes] = React.useState("");

  const approvalsQuery = useQuery({
    queryKey: queryKeys.approvals(includeDecided),
    queryFn: () => api.listApprovals(includeDecided),
  });

  const decisionMutation = useMutation({
    mutationFn: ({ pending, decision }: DecisionTarget) =>
      decision === "approve"
        ? api.approveApproval(pending.id, notes || undefined)
        : api.rejectApproval(pending.id, notes || undefined),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["approvals"] });
      queryClient.invalidateQueries({ queryKey: queryKeys.rollout });
      closeDialog();
    },
  });

  const closeDialog = () => {
    setTarget(null);
    setNotes("");
    decisionMutation.reset();
  };

  const approvals = approvalsQuery.data?.approvals ?? [];

  return (
    <div>
      <PageHeader
        title="Approvals"
        description="Pending AI proposals awaiting a supervised decision."
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => setIncludeDecided((v) => !v)}
          >
            {includeDecided ? "Show open only" : "Show all"}
          </Button>
        }
      />

      <QueryState
        isLoading={approvalsQuery.isLoading}
        isError={approvalsQuery.isError}
        error={approvalsQuery.error}
        isEmpty={approvals.length === 0}
        emptyMessage={
          includeDecided
            ? "No approvals on record."
            : "No open approvals. Nothing needs a decision right now."
        }
      >
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {approvals.map((p) => (
            <Card key={p.id} data-testid="approval-card">
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between gap-2">
                  <CardTitle className="flex items-center gap-2">
                    <span className="font-mono uppercase">{p.room_id}</span>
                    <span className="text-2xs font-normal text-muted-foreground">
                      #{p.id}
                    </span>
                  </CardTitle>
                  <PendingStatusBadge status={p.status} />
                </div>
                <p className="text-xs text-muted-foreground">
                  Created {formatTs(p.created_at)}
                  {p.status === "open" && p.expires_at
                    ? ` · expires in ${relativeAge(p.expires_at)}`
                    : null}
                </p>
              </CardHeader>
              <CardContent className="space-y-3 pt-0">
                <p className="text-sm">{p.summary}</p>
                <div className="flex flex-wrap items-center gap-2 text-2xs text-muted-foreground">
                  <Badge variant="secondary">
                    snapshot #{p.snapshot_id}
                  </Badge>
                  {p.llm_call_id != null ? (
                    <Badge variant="secondary">
                      llm call #{p.llm_call_id}
                    </Badge>
                  ) : null}
                  {p.decided_by ? (
                    <span>
                      decided by {p.decided_by} via{" "}
                      {p.decision_channel ?? "ui"}
                    </span>
                  ) : null}
                </div>
                {p.status === "open" ? (
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="success"
                      onClick={() =>
                        setTarget({ pending: p, decision: "approve" })
                      }
                    >
                      <Check className="h-3.5 w-3.5" />
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      onClick={() =>
                        setTarget({ pending: p, decision: "reject" })
                      }
                    >
                      <X className="h-3.5 w-3.5" />
                      Reject
                    </Button>
                  </div>
                ) : null}
              </CardContent>
            </Card>
          ))}
        </div>
      </QueryState>

      <Dialog
        open={target !== null}
        onClose={closeDialog}
        title={
          target?.decision === "approve"
            ? `Approve proposal #${target?.pending.id}`
            : `Reject proposal #${target?.pending.id}`
        }
        description={
          target?.decision === "approve"
            ? "The proposal is re-checked against current state, then applied as runtime overlays."
            : "The proposal is dismissed. No overlay is applied."
        }
        footer={
          <>
            <Button variant="outline" onClick={closeDialog}>
              Cancel
            </Button>
            <Button
              variant={
                target?.decision === "approve" ? "success" : "destructive"
              }
              disabled={decisionMutation.isPending}
              onClick={() => target && decisionMutation.mutate(target)}
            >
              {decisionMutation.isPending
                ? "Submitting…"
                : target?.decision === "approve"
                  ? "Confirm approve"
                  : "Confirm reject"}
            </Button>
          </>
        }
      >
        <label className="mb-1 block text-xs text-muted-foreground">
          Decision note (optional)
        </label>
        <Textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Recorded with the decision in the audit trail."
        />
        {decisionMutation.isError ? (
          <p className="mt-2 text-xs text-critical">
            {errorText(decisionMutation.error)}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}
