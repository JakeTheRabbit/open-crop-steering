"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  Download,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { AuditEventType } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState } from "@/components/query-state";
import { EventTypeBadge } from "@/components/status-badges";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Card, CardContent } from "@/components/ui/card";
import { formatTs } from "@/lib/utils";

const PAGE_SIZE = 50;

const EVENT_TYPES: AuditEventType[] = [
  "info_event",
  "controlled_adjustment",
  "system_warning",
  "guardrail_rejection",
  "formal_deviation",
  "critical_incident",
  "recipe_revision_created",
  "recipe_revision_approved",
  "runtime_adjustment_added",
  "runtime_adjustment_reverted",
  "user_role_changed",
  "rollout_advanced",
  "deviation_acknowledged",
  "audit_export",
  "hmac_key_rotated",
];

/** Chain-verify banner — recomputes every row's HMAC server-side. */
function ChainVerifyBanner() {
  const verifyQuery = useQuery({
    queryKey: queryKeys.auditVerify,
    queryFn: () => api.verifyAuditChain(),
    refetchInterval: false,
  });

  if (verifyQuery.isLoading) {
    return (
      <Card>
        <CardContent className="flex items-center gap-2 p-3 text-sm text-muted-foreground">
          <Spinner />
          Verifying audit chain…
        </CardContent>
      </Card>
    );
  }
  if (verifyQuery.isError || !verifyQuery.data) {
    return (
      <Card className="border-critical/40">
        <CardContent className="flex items-center gap-2 p-3 text-sm text-critical">
          <ShieldAlert className="h-4 w-4" />
          Chain verification could not run.
        </CardContent>
      </Card>
    );
  }
  const { ok, rows_checked, first_bad_id, message } = verifyQuery.data;
  return (
    <Card className={ok ? "border-healthy/40" : "border-critical/50"}>
      <CardContent className="flex items-center gap-2 p-3 text-sm">
        {ok ? (
          <ShieldCheck className="h-4 w-4 text-healthy" />
        ) : (
          <ShieldAlert className="h-4 w-4 text-critical" />
        )}
        <span className={ok ? "text-healthy" : "text-critical"}>
          {ok
            ? `Audit chain intact — ${rows_checked} rows verified.`
            : `Chain verification FAILED at row #${first_bad_id}. ${message}`}
        </span>
      </CardContent>
    </Card>
  );
}

/**
 * Audit event browser.
 *
 * A paginated, filterable view of the tamper-evident audit chain, the
 * server-side chain-verify status, and a CSV export. QAP-gated on the
 * backend; a non-QAP caller sees a 403 surfaced by {@link QueryState}.
 */
export default function AuditPage() {
  const [page, setPage] = React.useState(0);
  const [eventType, setEventType] = React.useState<AuditEventType | "">("");
  const [roomId, setRoomId] = React.useState("");
  const [exportStart, setExportStart] = React.useState("");
  const [exportEnd, setExportEnd] = React.useState("");

  const params = {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    event_type: eventType || undefined,
    room_id: roomId || undefined,
  };

  const eventsQuery = useQuery({
    queryKey: queryKeys.auditEvents(params),
    queryFn: () => api.listAuditEvents(params),
  });

  const events = eventsQuery.data?.events ?? [];
  const total = eventsQuery.data?.total ?? 0;
  const maxPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  const exportHref = api.auditExportUrl(
    exportStart || undefined,
    exportEnd || undefined,
  );

  return (
    <div>
      <PageHeader
        title="Audit Log"
        description="Tamper-evident HMAC-chained event record."
        actions={
          <Button asChild size="sm">
            {/* A real CSV download; the backend records the export itself. */}
            <a href={exportHref} download>
              <Download className="h-3.5 w-3.5" />
              Export CSV
            </a>
          </Button>
        }
      />

      <div className="mb-4 space-y-3">
        <ChainVerifyBanner />

        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="mb-1 block text-2xs uppercase tracking-wide text-muted-foreground">
              Event type
            </label>
            <Select
              value={eventType}
              onChange={(e) => {
                setEventType(e.target.value as AuditEventType | "");
                setPage(0);
              }}
              className="w-56"
            >
              <option value="">All event types</option>
              {EVENT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <label className="mb-1 block text-2xs uppercase tracking-wide text-muted-foreground">
              Room
            </label>
            <Input
              value={roomId}
              onChange={(e) => {
                setRoomId(e.target.value);
                setPage(0);
              }}
              placeholder="room id"
              className="w-32"
            />
          </div>
          <div className="ml-auto flex items-end gap-2">
            <div>
              <label className="mb-1 block text-2xs uppercase tracking-wide text-muted-foreground">
                Export from
              </label>
              <Input
                type="date"
                value={exportStart}
                onChange={(e) => setExportStart(e.target.value)}
                className="w-40"
              />
            </div>
            <div>
              <label className="mb-1 block text-2xs uppercase tracking-wide text-muted-foreground">
                to
              </label>
              <Input
                type="date"
                value={exportEnd}
                onChange={(e) => setExportEnd(e.target.value)}
                className="w-40"
              />
            </div>
          </div>
        </div>
      </div>

      <QueryState
        isLoading={eventsQuery.isLoading}
        isError={eventsQuery.isError}
        error={eventsQuery.error}
        isEmpty={events.length === 0}
        emptyMessage="No audit events match the current filters."
      >
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-44">When</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Actor</TableHead>
                <TableHead>Room</TableHead>
                <TableHead>Summary</TableHead>
                <TableHead className="w-20">Key</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((e) => (
                <TableRow key={e.id} data-testid="audit-row">
                  <TableCell className="tabular text-muted-foreground">
                    {e.id}
                  </TableCell>
                  <TableCell className="text-xs">
                    {formatTs(e.occurred_at)}
                  </TableCell>
                  <TableCell>
                    <EventTypeBadge type={e.event_type} />
                  </TableCell>
                  <TableCell className="text-xs">
                    {e.actor_id ?? "system"}
                  </TableCell>
                  <TableCell className="font-mono text-xs uppercase">
                    {e.room_id ?? "—"}
                  </TableCell>
                  <TableCell className="max-w-md text-xs">
                    {e.summary ?? "—"}
                    {e.reason_codes.length > 0 ? (
                      <span className="ml-1 text-muted-foreground">
                        [{e.reason_codes.join(", ")}]
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell
                    className="tabular text-2xs text-muted-foreground"
                    title={`hmac ${e.hmac.slice(0, 16)}…`}
                  >
                    #{e.key_id}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      </QueryState>

      <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
        <span className="flex items-center gap-1">
          <CheckCircle2 className="h-3.5 w-3.5" />
          {total} events · page {page + 1} of {maxPage + 1}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            Previous
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= maxPage}
            onClick={() => setPage((p) => Math.min(maxPage, p + 1))}
          >
            Next
          </Button>
        </div>
      </div>
    </div>
  );
}
