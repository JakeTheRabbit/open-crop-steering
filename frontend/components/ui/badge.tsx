import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-2xs font-medium leading-none transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-primary/15 text-primary",
        secondary: "border-transparent bg-muted text-muted-foreground",
        outline: "border-border text-foreground",
        healthy: "border-transparent bg-healthy/15 text-healthy",
        impaired: "border-transparent bg-impaired/15 text-impaired",
        critical: "border-transparent bg-critical/15 text-critical",
        warning: "border-transparent bg-impaired/15 text-impaired",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

/** Compact status pill. Variants cover room-state + severity semantics. */
function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
