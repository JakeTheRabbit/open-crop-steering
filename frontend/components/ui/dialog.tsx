"use client";

import * as React from "react";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * A minimal modal dialog.
 *
 * Deliberately dependency-free (no Radix) — controlled `open` / `onClose`,
 * backdrop click + Escape to dismiss, focus is moved into the panel. The
 * approvals + deviations pages use it for confirm flows.
 */
export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  description?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  className,
}: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null);

  // Keep the latest onClose without making it an effect dependency —
  // a parent that recreates onClose each render must not re-run the
  // focus effect (that would steal focus from inputs on every keystroke).
  const onCloseRef = React.useRef(onClose);
  React.useEffect(() => {
    onCloseRef.current = onClose;
  });

  // Escape-to-close: bound once while open.
  React.useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // Move focus into the panel exactly once, on the open transition.
  React.useEffect(() => {
    if (open) panelRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        className={cn(
          "relative w-full max-w-lg rounded-lg border border-border bg-card p-5 shadow-lg outline-none",
          className,
        )}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute right-3 top-3 rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
        {title ? (
          <h2 className="mb-1 pr-8 text-base font-semibold">{title}</h2>
        ) : null}
        {description ? (
          <p className="mb-3 text-sm text-muted-foreground">{description}</p>
        ) : null}
        <div className="text-sm">{children}</div>
        {footer ? (
          <div className="mt-5 flex justify-end gap-2">{footer}</div>
        ) : null}
      </div>
    </div>
  );
}
