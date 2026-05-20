import { PlannerPage } from "@/app/planner/recipes/[recipeId]/planner-page";

/**
 * Static-export params for `/planner/recipes/[recipeId]`.
 *
 * Recipes are runtime data, so the build cannot enumerate every id.
 * The page body is fully client-rendered and resolves the recipeId
 * from the URL via `useParams`, so a single prebuilt shell handles
 * every recipe at runtime. We export one `recipe` placeholder; FastAPI
 * serves the matching `index.html` (or this placeholder shell, whose
 * JS then loads the real recipe by id).
 *
 * Pattern mirrors `app/planner/[room]/page.tsx` from the legacy
 * per-room planner — same constraint applies (static export forbids
 * server-side per-request rendering).
 */
export function generateStaticParams(): { recipeId: string }[] {
  return [{ recipeId: "recipe" }];
}

export default function PlannerRecipePage() {
  return <PlannerPage />;
}
