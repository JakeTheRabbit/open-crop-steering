"use client";

import * as React from "react";
import { Check, ChevronsUpDown, X } from "lucide-react";

import type { HaArea, HaEntity } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

/**
 * A searchable Home Assistant entity selector.
 *
 * The HA registry holds ~8000 entities, so this is built as a filtered
 * popover rather than a plain `<select>`: free-text search plus area and
 * domain dropdowns narrow the list, and only the first {@link MAX_RESULTS}
 * matches render to keep the DOM light. Dependency-free (no Radix) to
 * match the rest of the UI kit — outside-click and Escape dismiss it.
 *
 * `value` is the selected `entity_id` (or `null`). The optional
 * `defaultAreaId` pre-seeds the area filter (the room's own area) while
 * still letting the user clear it.
 */

/** Cap rendered rows — the registry is far too large to list in full. */
const MAX_RESULTS = 200;

export interface EntityPickerProps {
  /** The currently-selected entity_id, or null when unassigned. */
  value: string | null;
  onChange: (entityId: string | null) => void;
  entities: HaEntity[];
  areas: HaArea[];
  /** Entity index keyed by entity_id — for resolving `value` to a label. */
  entityById: Map<string, HaEntity>;
  /** Pre-seed the area filter (typically the room's matching area). */
  defaultAreaId?: string | null;
  /** Restrict the domain filter's useful options (e.g. only "sensor"). */
  placeholder?: string;
  disabled?: boolean;
  /** Forwarded to the trigger button for test selection. */
  "data-testid"?: string;
}

/** Lower-cased substring match across the user-visible fields. */
function matchesText(e: HaEntity, q: string): boolean {
  if (!q) return true;
  const hay = `${e.name} ${e.entity_id} ${e.area ?? ""}`.toLowerCase();
  return hay.includes(q);
}

export function EntityPicker({
  value,
  onChange,
  entities,
  areas,
  entityById,
  defaultAreaId,
  placeholder = "Select an entity…",
  disabled = false,
  "data-testid": testId,
}: EntityPickerProps) {
  const [open, setOpen] = React.useState(false);
  const [text, setText] = React.useState("");
  const [areaFilter, setAreaFilter] = React.useState<string>("");
  const [domainFilter, setDomainFilter] = React.useState<string>("");
  const rootRef = React.useRef<HTMLDivElement>(null);
  const searchRef = React.useRef<HTMLInputElement>(null);

  // Seed the area filter from the room's area the first time the picker
  // opens; the user can still clear it back to "all areas".
  const seededRef = React.useRef(false);
  React.useEffect(() => {
    if (open && !seededRef.current) {
      seededRef.current = true;
      if (defaultAreaId) setAreaFilter(defaultAreaId);
      // Defer focus until the panel has painted.
      requestAnimationFrame(() => searchRef.current?.focus());
    }
    if (!open) seededRef.current = false;
  }, [open, defaultAreaId]);

  // Outside-click + Escape dismiss.
  React.useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  /** Distinct domains present in the registry, sorted. */
  const domains = React.useMemo(() => {
    const set = new Set<string>();
    for (const e of entities) set.add(e.domain);
    return [...set].sort();
  }, [entities]);

  /** Filtered, capped result list. */
  const { results, totalMatches } = React.useMemo(() => {
    const q = text.trim().toLowerCase();
    const out: HaEntity[] = [];
    let total = 0;
    for (const e of entities) {
      // areaFilter holds an area_id (the <Select> option value and the
      // room's seeded default are both ids) — match on e.area_id, not
      // the display name in e.area.
      if (areaFilter && e.area_id !== areaFilter) continue;
      if (domainFilter && e.domain !== domainFilter) continue;
      if (!matchesText(e, q)) continue;
      total += 1;
      if (out.length < MAX_RESULTS) out.push(e);
    }
    out.sort((a, b) => a.name.localeCompare(b.name));
    return { results: out, totalMatches: total };
  }, [entities, text, areaFilter, domainFilter]);

  const selected = value ? entityById.get(value) ?? null : null;

  const select = (entityId: string | null) => {
    onChange(entityId);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className="relative">
      <div className="flex items-center gap-1">
        <button
          type="button"
          data-testid={testId}
          disabled={disabled}
          onClick={() => setOpen((o) => !o)}
          className={cn(
            "flex h-9 w-full items-center justify-between gap-2 rounded-md border border-input bg-background px-3 py-1 text-left text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
          )}
        >
          {selected ? (
            <span className="min-w-0 flex-1 truncate">
              <span className="font-medium">{selected.name}</span>
              <span className="ml-1.5 font-mono text-2xs text-muted-foreground">
                {selected.entity_id}
              </span>
            </span>
          ) : value ? (
            // The id is set but missing from the registry (stale entity).
            <span className="min-w-0 flex-1 truncate font-mono text-2xs text-impaired">
              {value} (not in registry)
            </span>
          ) : (
            <span className="flex-1 truncate text-muted-foreground">
              {placeholder}
            </span>
          )}
          <ChevronsUpDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        </button>
        {value ? (
          <Button
            type="button"
            size="icon"
            variant="ghost"
            disabled={disabled}
            aria-label="Clear entity"
            className="h-9 w-9 shrink-0"
            onClick={() => onChange(null)}
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        ) : null}
      </div>

      {open ? (
        <div className="absolute z-40 mt-1 w-full min-w-[20rem] rounded-md border border-border bg-card shadow-lg">
          <div className="space-y-2 border-b border-border p-2">
            <Input
              ref={searchRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Search name, entity_id, area…"
            />
            <div className="flex gap-2">
              <Select
                value={areaFilter}
                onChange={(e) => setAreaFilter(e.target.value)}
                aria-label="Filter by area"
              >
                <option value="">All areas</option>
                {areas.map((a) => (
                  <option key={a.area_id} value={a.area_id}>
                    {a.name}
                  </option>
                ))}
              </Select>
              <Select
                value={domainFilter}
                onChange={(e) => setDomainFilter(e.target.value)}
                aria-label="Filter by domain"
              >
                <option value="">All domains</option>
                {domains.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          <div className="max-h-72 overflow-y-auto py-1">
            {value ? (
              <button
                type="button"
                onClick={() => select(null)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted"
              >
                <X className="h-3.5 w-3.5" />
                Clear selection
              </button>
            ) : null}

            {results.length === 0 ? (
              <p className="px-3 py-3 text-xs text-muted-foreground">
                No entities match these filters.
              </p>
            ) : (
              results.map((e) => {
                const isSel = e.entity_id === value;
                return (
                  <button
                    key={e.entity_id}
                    type="button"
                    onClick={() => select(e.entity_id)}
                    className={cn(
                      "flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-muted",
                      isSel && "bg-primary/10",
                    )}
                  >
                    <Check
                      className={cn(
                        "mt-0.5 h-3.5 w-3.5 shrink-0",
                        isSel ? "text-primary" : "text-transparent",
                      )}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">
                        {e.name}
                      </span>
                      <span className="block truncate font-mono text-2xs text-muted-foreground">
                        {e.entity_id}
                      </span>
                      <span className="mt-0.5 flex flex-wrap gap-x-2 text-2xs text-muted-foreground">
                        <span>{e.domain}</span>
                        {e.area ? <span>· {e.area}</span> : null}
                        {e.state != null ? (
                          <span>
                            · {e.state}
                            {e.unit ? ` ${e.unit}` : ""}
                          </span>
                        ) : null}
                      </span>
                    </span>
                  </button>
                );
              })
            )}

            {totalMatches > results.length ? (
              <p className="px-3 py-2 text-2xs text-muted-foreground">
                Showing {results.length} of {totalMatches} matches — refine
                the search to narrow down.
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export interface MultiEntityPickerProps {
  /** The currently-selected entity_ids (may be empty). */
  values: string[];
  onChange: (next: string[]) => void;
  entities: HaEntity[];
  areas: HaArea[];
  entityById: Map<string, HaEntity>;
  defaultAreaId?: string | null;
  disabled?: boolean;
}

/**
 * Assign *several* HA entities to one role.
 *
 * A room routinely has multiple AC units, grow-light circuits, etc.
 * This wraps {@link EntityPicker}: the selected entities show as
 * removable rows, and a single picker below adds another (duplicates
 * are ignored).
 */
export function MultiEntityPicker({
  values,
  onChange,
  entities,
  areas,
  entityById,
  defaultAreaId,
  disabled = false,
}: MultiEntityPickerProps) {
  const add = (entityId: string | null) => {
    if (entityId && !values.includes(entityId)) {
      onChange([...values, entityId]);
    }
  };
  const remove = (entityId: string) =>
    onChange(values.filter((v) => v !== entityId));

  return (
    <div className="space-y-1.5">
      {values.map((id) => {
        const e = entityById.get(id);
        return (
          <div
            key={id}
            className="flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5"
          >
            <span className="min-w-0 flex-1 truncate text-sm">
              {e ? (
                <>
                  <span className="font-medium">{e.name}</span>
                  <span className="ml-1.5 font-mono text-2xs text-muted-foreground">
                    {e.entity_id}
                  </span>
                </>
              ) : (
                <span className="font-mono text-2xs text-impaired">
                  {id} (not in registry)
                </span>
              )}
            </span>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              disabled={disabled}
              aria-label={`Remove ${id}`}
              className="h-7 w-7 shrink-0"
              onClick={() => remove(id)}
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        );
      })}
      <EntityPicker
        value={null}
        onChange={add}
        entities={entities}
        areas={areas}
        entityById={entityById}
        defaultAreaId={defaultAreaId}
        disabled={disabled}
        placeholder={
          values.length > 0 ? "Add another entity…" : "Select an entity…"
        }
      />
    </div>
  );
}
