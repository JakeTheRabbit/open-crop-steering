"use client";

import * as React from "react";
import {
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";

import { ApiError } from "@/lib/api-client";

/**
 * App-wide React-Query provider.
 *
 * Defaults tuned for an operator control panel: a short stale time so
 * data is reasonably fresh, a 30s background refetch for live-ish
 * dashboards, and no retry on 4xx (a 403/404 will not fix itself).
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 15_000,
            refetchInterval: 30_000,
            refetchOnWindowFocus: true,
            retry: (failureCount, error) => {
              if (
                error instanceof ApiError &&
                error.status >= 400 &&
                error.status < 500
              ) {
                return false;
              }
              return failureCount < 2;
            },
          },
          mutations: {
            retry: false,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}
