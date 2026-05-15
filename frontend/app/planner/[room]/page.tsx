import { PlannerClient } from "@/app/planner/[room]/planner-client";

/**
 * Static-export params for `/planner/[room]`.
 *
 * Rooms are runtime data, so the build cannot know every id. The page
 * body is fully client-rendered and resolves the room from the URL via
 * `useParams`, so the prebuilt HTML shell works for any room. We export
 * a small set of common room ids plus a `room` placeholder; FastAPI
 * serves the matching `index.html` (or the placeholder shell, whose JS
 * then loads the real room's recipe).
 */
export function generateStaticParams(): { room: string }[] {
  return [
    { room: "room" },
    { room: "F1" },
    { room: "F2" },
    { room: "F3" },
    { room: "F4" },
    { room: "F5" },
    { room: "F6" },
  ];
}

export default function PlannerRoomPage() {
  return <PlannerClient />;
}
