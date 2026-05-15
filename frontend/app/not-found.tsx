import Link from "next/link";

import { Button } from "@/components/ui/button";

/** 404 page for an unknown route. */
export default function NotFound() {
  return (
    <div className="flex flex-col items-start gap-4 py-16">
      <div>
        <h1 className="text-lg font-semibold">Page not found</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          That route does not exist in the grow-control UI.
        </p>
      </div>
      <Button asChild variant="outline" size="sm">
        <Link href="/">Back to dashboard</Link>
      </Button>
    </div>
  );
}
