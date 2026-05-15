import type { Metadata, Viewport } from "next";

import "./globals.css";

import { Nav } from "@/components/nav";
import { Providers } from "@/app/providers";

export const metadata: Metadata = {
  title: "Open Crop Steering — Grow Control",
  description:
    "AI-supervised cannabis cultivation control plane (operator UI).",
};

export const viewport: Viewport = {
  themeColor: "#0f1419",
  width: "device-width",
  initialScale: 1,
};

/**
 * Root layout.
 *
 * Dark-by-default (`className="dark"` on <html>). A persistent left nav
 * rail plus a scrollable content column. `Providers` wraps the tree in a
 * TanStack Query client.
 */
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body>
        <Providers>
          <div className="flex min-h-screen">
            <aside className="hidden w-56 shrink-0 border-r border-border bg-card md:block">
              <div className="sticky top-0">
                <Nav />
              </div>
            </aside>
            <main className="min-w-0 flex-1 px-4 py-5 md:px-8 md:py-7">
              <div className="mx-auto max-w-7xl">{children}</div>
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
