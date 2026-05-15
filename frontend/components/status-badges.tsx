import { Badge } from "@/components/ui/badge";
import type {
  AuditEventType,
  EventSeverity,
  PendingStatus,
  RoomState,
} from "@/lib/types";

/**
 * Room health badge — HEALTHY / IMPAIRED / CRITICAL.
 *
 * The backend stores `current_state` lower-cased; the operator UI
 * presents it upper-cased per the plan's dashboard spec.
 */
export function RoomStateBadge({ state }: { state: RoomState }) {
  const variant =
    state === "healthy"
      ? "healthy"
      : state === "impaired"
        ? "impaired"
        : "critical";
  return <Badge variant={variant}>{state.toUpperCase()}</Badge>;
}

/** Event-severity badge — info / warning / critical. */
export function SeverityBadge({ severity }: { severity: EventSeverity }) {
  const variant =
    severity === "critical"
      ? "critical"
      : severity === "warning"
        ? "warning"
        : "secondary";
  return <Badge variant={variant}>{severity}</Badge>;
}

/** SFW pending-status badge. */
export function PendingStatusBadge({ status }: { status: PendingStatus }) {
  const variant =
    status === "approved"
      ? "healthy"
      : status === "rejected"
        ? "critical"
        : status === "expired"
          ? "secondary"
          : "default";
  return <Badge variant={variant}>{status}</Badge>;
}

/** Audit event-type chip; deviation-grade types read as critical. */
export function EventTypeBadge({ type }: { type: AuditEventType }) {
  const critical: AuditEventType[] = [
    "formal_deviation",
    "critical_incident",
  ];
  const warning: AuditEventType[] = [
    "system_warning",
    "guardrail_rejection",
  ];
  const variant = critical.includes(type)
    ? "critical"
    : warning.includes(type)
      ? "warning"
      : "outline";
  return <Badge variant={variant}>{type}</Badge>;
}
