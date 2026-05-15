"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, UserPlus } from "lucide-react";

import { api } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { AdminUser, RoleName, UserUpsertBody } from "@/lib/types";
import { PageHeader } from "@/components/page-header";
import { QueryState, errorText } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
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

const ALL_ROLES: RoleName[] = ["operator", "cultivator", "qap", "admin"];

/** The role-name colour ladder. */
function roleVariant(role: RoleName) {
  switch (role) {
    case "admin":
      return "critical" as const;
    case "qap":
      return "warning" as const;
    case "cultivator":
      return "default" as const;
    default:
      return "secondary" as const;
  }
}

interface EditorState {
  mode: "create" | "edit";
  draft: UserUpsertBody;
}

function blankDraft(): UserUpsertBody {
  return {
    id: "",
    display_name: "",
    email: "",
    active: true,
    roles: ["operator"],
  };
}

/**
 * User + role management (admin-only on the backend).
 *
 * Lists users with their resolved roles and provides a create / edit
 * dialog. The role checkboxes set the full desired role set; the
 * backend reconciles grants/revocations and writes a
 * `user_role_changed` audit row per change.
 */
export default function AdminUsersPage() {
  const queryClient = useQueryClient();
  const [editor, setEditor] = React.useState<EditorState | null>(null);

  const usersQuery = useQuery({
    queryKey: queryKeys.users,
    queryFn: () => api.listUsers(),
  });

  const upsertMutation = useMutation({
    mutationFn: (body: UserUpsertBody) => api.upsertUser(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.users });
      setEditor(null);
    },
  });

  const users = usersQuery.data?.users ?? [];

  const openCreate = () =>
    setEditor({ mode: "create", draft: blankDraft() });
  const openEdit = (u: AdminUser) =>
    setEditor({
      mode: "edit",
      draft: {
        id: u.id,
        display_name: u.display_name,
        email: u.email ?? "",
        active: u.active,
        roles: [...u.roles],
      },
    });

  const patchDraft = (patch: Partial<UserUpsertBody>) =>
    setEditor((prev) =>
      prev ? { ...prev, draft: { ...prev.draft, ...patch } } : prev,
    );

  const toggleRole = (role: RoleName) => {
    setEditor((prev) => {
      if (!prev) return prev;
      const has = prev.draft.roles.includes(role);
      const roles = has
        ? prev.draft.roles.filter((r) => r !== role)
        : [...prev.draft.roles, role];
      return { ...prev, draft: { ...prev.draft, roles } };
    });
  };

  const draftValid =
    !!editor &&
    editor.draft.id.trim().length > 0 &&
    editor.draft.display_name.trim().length > 0;

  return (
    <div>
      <PageHeader
        title="Users & Roles"
        description="Access control — operator, cultivator, QAP, admin."
        actions={
          <Button size="sm" onClick={openCreate}>
            <UserPlus className="h-3.5 w-3.5" />
            Add user
          </Button>
        }
      />

      <QueryState
        isLoading={usersQuery.isLoading}
        isError={usersQuery.isError}
        error={usersQuery.error}
        isEmpty={users.length === 0}
        emptyMessage="No users yet. Add the first user to grant access."
      >
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>User</TableHead>
                <TableHead>HA user id</TableHead>
                <TableHead>Roles</TableHead>
                <TableHead className="w-20">Active</TableHead>
                <TableHead className="w-36">Created</TableHead>
                <TableHead className="w-20 text-right">Edit</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((u) => (
                <TableRow key={u.id} data-testid="user-row">
                  <TableCell>
                    <div className="text-sm font-medium">
                      {u.display_name}
                    </div>
                    {u.email ? (
                      <div className="text-2xs text-muted-foreground">
                        {u.email}
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="font-mono text-2xs text-muted-foreground">
                    {u.id}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {u.roles.length === 0 ? (
                        <span className="text-2xs text-muted-foreground">
                          none
                        </span>
                      ) : (
                        u.roles.map((r) => (
                          <Badge key={r} variant={roleVariant(r)}>
                            {r}
                          </Badge>
                        ))
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={u.active ? "healthy" : "secondary"}>
                      {u.active ? "active" : "disabled"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatTs(u.created_at)}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={`Edit ${u.display_name}`}
                      onClick={() => openEdit(u)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      </QueryState>

      <Dialog
        open={editor !== null}
        onClose={() => setEditor(null)}
        title={editor?.mode === "create" ? "Add user" : "Edit user"}
        footer={
          <>
            <Button variant="outline" onClick={() => setEditor(null)}>
              Cancel
            </Button>
            <Button
              disabled={!draftValid || upsertMutation.isPending}
              onClick={() => editor && upsertMutation.mutate(editor.draft)}
            >
              {upsertMutation.isPending ? "Saving…" : "Save user"}
            </Button>
          </>
        }
      >
        {editor ? (
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">
                HA user id
              </label>
              <Input
                value={editor.draft.id}
                disabled={editor.mode === "edit"}
                onChange={(e) => patchDraft({ id: e.target.value })}
                placeholder="e.g. a1b2c3d4…"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">
                Display name
              </label>
              <Input
                value={editor.draft.display_name}
                onChange={(e) =>
                  patchDraft({ display_name: e.target.value })
                }
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">
                Email (optional)
              </label>
              <Input
                type="email"
                value={editor.draft.email ?? ""}
                onChange={(e) => patchDraft({ email: e.target.value })}
              />
            </div>
            <div>
              <span className="mb-1 block text-xs text-muted-foreground">
                Roles
              </span>
              <div className="flex flex-wrap gap-3">
                {ALL_ROLES.map((role) => (
                  <label
                    key={role}
                    className="flex items-center gap-1.5 text-sm"
                  >
                    <input
                      type="checkbox"
                      checked={editor.draft.roles.includes(role)}
                      onChange={() => toggleRole(role)}
                      className="accent-[hsl(var(--primary))]"
                    />
                    {role}
                  </label>
                ))}
              </div>
            </div>
            <label className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={editor.draft.active}
                onChange={(e) => patchDraft({ active: e.target.checked })}
                className="accent-[hsl(var(--primary))]"
              />
              Account active
            </label>
            {upsertMutation.isError ? (
              <p className="text-xs text-critical">
                {errorText(upsertMutation.error)}
              </p>
            ) : null}
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}
