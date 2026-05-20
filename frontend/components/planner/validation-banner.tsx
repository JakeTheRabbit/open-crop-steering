"use client";

import * as React from "react";
import { AlertCircle, AlertTriangle, ChevronDown, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { Finding, ValidationResult } from "@/lib/planner/validator";

/**
 * Fixed-top validation banner.
 *
 * Shows the hard / warning counts and (when expanded) one row per
 * finding. Clicking a finding fires `onFindingClick` so the canvas
 * can scroll-to / select the offending phase or day.
 *
 * Stays absent when there are zero findings — empty state is "no
 * banner at all" rather than "0 findings" which would be noise.
 */
export interface ValidationBannerProps {
  findings: ValidationResult;
  onFindingClick?: (finding: Finding) => void;
}

export function ValidationBanner({
  findings,
  onFindingClick,
}: ValidationBannerProps) {
  const [expanded, setExpanded] = React.useState(false);

  const hardCount = findings.hardRefusals.length;
  const warnCount = findings.warnings.length;
  const total = hardCount + warnCount;

  if (total === 0) return null;

  const allFindings: Finding[] = [
    ...findings.hardRefusals,
    ...findings.warnings,
  ];

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="validation-banner"
      className={cn(
        "rounded-md border px-3 py-2 text-xs",
        hardCount > 0
          ? "border-critical/40 bg-critical/10 text-critical"
          : "border-impaired/40 bg-impaired/10 text-impaired",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {hardCount > 0 ? (
            <AlertCircle className="h-4 w-4 shrink-0" />
          ) : (
            <AlertTriangle className="h-4 w-4 shrink-0" />
          )}
          <span
            className="font-medium"
            data-testid="validation-banner-summary"
          >
            {hardCount > 0
              ? `${hardCount} hard finding${hardCount === 1 ? "" : "s"}`
              : null}
            {hardCount > 0 && warnCount > 0 ? " · " : null}
            {warnCount > 0
              ? `${warnCount} warning${warnCount === 1 ? "" : "s"}`
              : null}
          </span>
        </div>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-controls="validation-banner-list"
          className="h-6 px-2 text-2xs"
        >
          {expanded ? (
            <>
              <ChevronDown className="h-3 w-3" /> Hide
            </>
          ) : (
            <>
              <ChevronRight className="h-3 w-3" /> View all
            </>
          )}
        </Button>
      </div>
      {expanded ? (
        <ul
          id="validation-banner-list"
          data-testid="validation-banner-list"
          className="mt-2 space-y-1 border-t border-current/20 pt-2"
        >
          {allFindings.map((finding, i) => (
            <li
              key={`${finding.code}-${i}`}
              data-testid="validation-finding-item"
              data-code={finding.code}
              data-hard={finding.hard}
            >
              <button
                type="button"
                onClick={() => onFindingClick?.(finding)}
                className="flex w-full items-start gap-2 rounded-sm px-1 py-0.5 text-left text-2xs hover:bg-current/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-current"
              >
                <span
                  className={cn(
                    "mt-0.5 inline-flex h-3 shrink-0 items-center rounded px-1 text-[9px] font-semibold uppercase",
                    finding.hard
                      ? "bg-critical/30 text-critical"
                      : "bg-impaired/30 text-impaired",
                  )}
                >
                  {finding.hard ? "Hard" : "Warn"}
                </span>
                <span className="flex-1 leading-snug">{finding.message}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
