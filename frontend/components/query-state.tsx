"use client";

import * as React from "react";
import { AlertTriangle, Inbox } from "lucide-react";

import { ApiError } from "@/lib/api-client";
import { Spinner } from "@/components/ui/spinner";

interface QueryStateProps {
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  /** True when the query succeeded but returned nothing to show. */
  isEmpty?: boolean;
  emptyMessage?: string;
  /** Optional notice for a 404 — used for not-yet-exposed endpoints. */
  notFoundMessage?: string;
  children: React.ReactNode;
}

/** Human-readable text for an error, preferring the API `detail`. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (typeof error.detail === "string") return error.detail;
    return `Request failed (HTTP ${error.status}).`;
  }
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

/**
 * Render-prop-free wrapper that resolves the standard query lifecycle —
 * loading, error, empty — to consistent UI, falling through to
 * `children` only on a populated success. Every data page uses it so
 * the operator sees the same affordances everywhere.
 */
export function QueryState({
  isLoading,
  isError,
  error,
  isEmpty = false,
  emptyMessage = "Nothing to show.",
  notFoundMessage,
  children,
}: QueryStateProps) {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 px-1 py-8 text-sm text-muted-foreground">
        <Spinner />
        Loading…
      </div>
    );
  }

  if (isError) {
    const is404 = error instanceof ApiError && error.status === 404;
    if (is404 && notFoundMessage) {
      return (
        <div className="flex items-start gap-2 rounded-lg border border-dashed border-border px-4 py-8 text-sm text-muted-foreground">
          <Inbox className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{notFoundMessage}</span>
        </div>
      );
    }
    return (
      <div className="flex items-start gap-2 rounded-lg border border-critical/40 bg-critical/10 px-4 py-3 text-sm text-critical">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <span>{errorText(error)}</span>
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-dashed border-border px-4 py-8 text-sm text-muted-foreground">
        <Inbox className="h-4 w-4" />
        {emptyMessage}
      </div>
    );
  }

  return <>{children}</>;
}
