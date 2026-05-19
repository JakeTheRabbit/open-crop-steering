"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  CheckSquare,
  Cannabis,
  LayoutDashboard,
  ListChecks,
  Plug,
  ScrollText,
  ShieldAlert,
  SlidersHorizontal,
  Users,
  Clock,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /** Match nested routes (e.g. /planner/[room]) as active. */
  prefix?: boolean;
}

const NAV: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/planner", label: "Recipe Planner", icon: SlidersHorizontal, prefix: true },
  { href: "/approvals", label: "Approvals", icon: CheckSquare },
  { href: "/deviations", label: "Deviations", icon: ShieldAlert },
  { href: "/audit", label: "Audit Log", icon: ScrollText },
];

const ADMIN_NAV: NavItem[] = [
  { href: "/admin/users", label: "Users & Roles", icon: Users },
  { href: "/admin/rooms", label: "Rooms & Equipment", icon: Plug },
  { href: "/admin/guardrails", label: "Guardrails", icon: ListChecks },
  { href: "/admin/no-touch-windows", label: "No-Touch Windows", icon: Clock },
];

function isActive(pathname: string, item: NavItem): boolean {
  if (item.href === "/") return pathname === "/";
  if (item.prefix) return pathname.startsWith(item.href);
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}

function NavLink({ item, pathname }: { item: NavItem; pathname: string }) {
  const active = isActive(pathname, item);
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors",
        active
          ? "bg-primary/15 font-medium text-primary"
          : "text-muted-foreground hover:bg-muted hover:text-foreground",
      )}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {item.label}
    </Link>
  );
}

/** Left navigation rail. */
export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="flex h-full flex-col gap-1 p-3">
      <Link href="/" className="mb-4 flex items-center gap-2 px-1.5 py-1">
        <Cannabis className="h-5 w-5 text-primary" />
        <div className="leading-tight">
          <div className="text-sm font-semibold">Open Crop Steering</div>
          <div className="text-2xs text-muted-foreground">Grow Control</div>
        </div>
      </Link>

      {NAV.map((item) => (
        <NavLink key={item.href} item={item} pathname={pathname} />
      ))}

      <div className="mt-4 px-2.5 text-2xs font-semibold uppercase tracking-wide text-muted-foreground">
        Admin
      </div>
      {ADMIN_NAV.map((item) => (
        <NavLink key={item.href} item={item} pathname={pathname} />
      ))}
    </nav>
  );
}
