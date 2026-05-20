# Planner Redesign — Design Spec

Spec produced by the Plan agent on 2026-05-20 for the **phase-bound per-day
recipe planner** that replaces the legacy per-day-grid planner. The
backend support for this model shipped in commit `b0d8162` (passes 5b + 6
of the AiGrowApp schema alignment).

This document is the contract for the implementing agent(s). It is
intentionally opinionated — defaults are picked, alternatives are listed
in §10 only when a judgment call is genuinely open.

---

## 1. Bottom-line summary

The rebuilt planner is a **phase-strip timeline with a day inspector**:
the canvas is a horizontal Gantt-style ribbon where each phase is a
coloured block running across its day range, day-override pins appear as
small markers above the timeline, and clicking either a phase or a day
opens a context-sensitive editor in the side panel. Phases are the
structural backbone (because that's what the data model is); per-day pins
are exception markers on top (because that's what overrides are). The
legacy 84×11 spreadsheet is retired — it scrolls forever, hides the phase
structure, and made the operator type the same number 7 times for each
week of a stage. The new planner edits a phase's defaults once and only
stamps a per-day override when the grower genuinely wants to deviate.

The recipe is treated as a **batched, validated draft** with explicit
Save / Discard rather than the per-pick optimistic style of the
sensor-role-picker. Recipe edits are interdependent (moving a phase
boundary changes which days belong where, which makes pin orphan-ness
depend on the in-flight reshape), so the planner holds a single draft in
a `useReducer`, runs validation client-side on every change, and commits
via two coordinated PUTs to `/grow-recipes/{id}` and
`/grow-recipes/{id}/day-overrides` only when the user explicitly clicks
Save.

---

## 2. Information architecture

```
/planner                                  recipe list (replaces today's per-room picker)
  └─ /planner/recipes/[recipeId]          the planner canvas (the main work surface)
      ├─ main: <PhaseTimeline>            phase ribbon, day axis, pin markers
      ├─ side: <PhaseEditor>              one selected; param-default table, light cycle, env band
      │   └─ <PhaseTargetRow> (per param)
      ├─ side: <DayInspector>             one selected; resolved values + pin controls
      │   └─ <DayParamRow> (per param resolved for that day)
      ├─ overlay: <SplitPhaseDialog>      "split phase at day N"
      ├─ overlay: <AddPinDialog>          "add pin for paramX on day N"
      ├─ overlay: <DiscardChangesDialog>  "you have N unsaved edits"
      ├─ overlay: <NewRecipeDialog>       launched from /planner; pick preset or blank
      ├─ banner: <ValidationBanner>       fixed top, sticky; "3 findings"
      └─ footer: <RecipeSaveBar>          sticky bottom; "Save · Discard · Undo"
```

The selected-phase panel and the day-inspector panel **share** the right
side and toggle: clicking a day opens the inspector, clicking a phase
header opens its editor. Multi-select is out of scope for pass 1.

Routing: `app/planner/page.tsx` (recipe list — replaces today's room
picker; recipes are no longer per-room in the new model),
`app/planner/recipes/[recipeId]/page.tsx` (planner shell),
`app/planner/recipes/new/page.tsx` (new-from-preset wizard, optional —
can be a dialog instead).

Today's `/planner` page renders rooms; the new model has recipes as a
first-class entity referenced from a batch's `growRecipe` field. The
room-keyed `/planner/[room]/...` path stays as a redirect that resolves
the room's active batch → its recipe → opens `/planner/recipes/{id}` (in
a follow-up pass; pass 1 just changes `/planner` to a recipe list).

---

## 3. Visual mockup

### Main planner canvas — phase selected

```
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ Recipe Planner — Cannabis 12-week (Blue Dream)                                               │
│ 84 day cycle · 5 phases · 11 parameters · 12 day-overrides           [Validate] [Save draft] │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ ! 1 hard finding   2 warnings                                                    [View all]  │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                              │
│  Day axis                                                                                    │
│  1       14      28      42      56      70      84                                          │
│  ├───────┼───────┼───────┼───────┼───────┼───────┤                                           │
│                                                                                              │
│  Pins        ●        ▲ ▲          ●●           ■    ●                                       │
│  (env/light/irrig/nut shapes — colour by param category)                                     │
│                                                                                              │
│ ┌─────────┬─────────────┬───────────────────────┬─────────────────┬─────────────────────┐    │
│ │ EARLY   │  LATE VEG   │     FLOWER STRETCH    │   FLOWER BULK   │   RIPEN / FLUSH     │    │
│ │  VEG    │             │                       │                 │                     │    │
│ │  d1-21  │   d22-42    │      d43-56           │     d57-77      │       d78-84        │    │
│ │  (21d)  │   (21d)     │      (14d)            │     (21d)       │       (7d)          │    │
│ │ ░░░░░░░ │ ▒▒▒▒▒▒▒▒▒▒▒ │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  │ ████████████████ │  ▓░░░░░░░░░░░░░░    │    │
│ │ green   │ lime        │  amber                │ orange          │   red               │    │
│ └─────────┴∥────────────┴∥──────────────────────┴∥────────────────┴∥────────────────────┘    │
│           ↑              ↑                       ↑                  ↑                        │
│           drag handles between phases (∥ = grabbable boundary)                               │
│                                                                                              │
│  [+ Phase before]  [+ Phase after]                                                           │
│                                                                                              │
├────────────────────────────────────┬─────────────────────────────────────────────────────────┤
│ SELECTED PHASE: Late Veg (d22-42)  │                                                         │
│ ────────────────────────────────── │  (day inspector hidden — no day selected)               │
│ Name      [Late Veg            ]   │                                                         │
│ Duration  [21] days                │  Click a day on the axis above to inspect or pin.       │
│ Order     2                        │                                                         │
│                                    │                                                         │
│ Light cycle                        │                                                         │
│  Hours on  [18]  hours off [6]     │                                                         │
│                                    │                                                         │
│ Environmental band (Convex)        │                                                         │
│  temp °C   min [22]  max [26]      │                                                         │
│  RH %      min [55]  max [65]      │                                                         │
│  CO2 ppm   min [800] max [1000]    │                                                         │
│  VPD kPa   min [0.9] max [1.2]     │                                                         │
│                                    │                                                         │
│ Parameter defaults                 │                                                         │
│ ─────────────────────────────────  │                                                         │
│ param          value   tol    unit │                                                         │
│ temp_day      [24]    [1]   °C [×] │                                                         │
│ temp_night    [20]    [1]   °C [×] │                                                         │
│ rh_day        [60]    [3]   %  [×] │                                                         │
│ rh_night      [62]    [3]   %  [×] │                                                         │
│ co2_day       [900]   [50]  ppm[×] │                                                         │
│ vpd_day       [1.1]   [0.1] kPa[×] │                                                         │
│ ppfd          [650]   [30]  µm [×] │                                                         │
│ photoperiod_h [18]    [-]   h  [×] │                                                         │
│ vwc_target    [55]    [3]   %  [×] │                                                         │
│ ec_target     [2.4]   [0.2] mS [×] │                                                         │
│ dryback_pct   [12]    [2]   %  [×] │                                                         │
│ [+ Add parameter ▼]                │                                                         │
│                                    │                                                         │
│ [Split phase here…]  [Delete]      │                                                         │
└────────────────────────────────────┴─────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│   3 unsaved edits      [↶ Undo]  [Discard…]                          [Save draft]            │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Day inspector — day 49 selected (pin existing)

```
┌────────────────────────────────────┬─────────────────────────────────────────────────────────┐
│ SELECTED PHASE: Flower Stretch     │ DAY 49  (week 7 · phase Flower Stretch · d7 of 14)      │
│ d43-56 (14d)                       │ ──────────────────────────────────────────────────────  │
│                                    │ Effective targets:                                      │
│ (phase form collapsed — click      │                                                         │
│ phase header to expand)            │ param          value    tol    unit    source           │
│                                    │ temp_day       26.0     1      °C      [default]        │
│                                    │ temp_night     21.0     1      °C      [default]        │
│                                    │ rh_day         55.0     3      %       [PINNED] [edit][×]│
│                                    │ rh_night       57.0     3      %       [default]        │
│                                    │ co2_day        1100     50     ppm     [PINNED] [edit][×]│
│                                    │ vpd_day        1.30     0.1    kPa     [default]        │
│                                    │ ppfd           850      30     µm/s    [default]        │
│                                    │ photoperiod_h  12       -      h       [default]        │
│                                    │ vwc_target     50.0     3      %       [default]        │
│                                    │ ec_target      2.8      0.2    mS/cm   [default]        │
│                                    │ dryback_pct    18.0     2      %       [default]        │
│                                    │                                                         │
│                                    │ [+ Pin another parameter…]                              │
│                                    │                                                         │
│                                    │ Pins on this day: 2                                     │
│                                    │ Drop a pin to revert that param to the phase default.   │
└────────────────────────────────────┴─────────────────────────────────────────────────────────┘
```

### Split phase dialog (overlay above main canvas)

```
┌─────────────────────────────────────────────────────────────┐
│ Split phase  "Flower Stretch"                           [×] │
├─────────────────────────────────────────────────────────────┤
│ Phase currently runs day 43 → day 56 (14 days).             │
│                                                             │
│ Split at day  [ 50 ]    (must be 44–55 inclusive)           │
│                                                             │
│ ┌──────────────────────┬──────────────────────────────────┐ │
│ │ Flower Stretch       │ New phase                        │ │
│ │ d43–50  (8d)         │ d51–56  (6d)                     │ │
│ │ Inherits all params  │ Inherits all params from parent  │ │
│ └──────────────────────┴──────────────────────────────────┘ │
│                                                             │
│ New phase name [Flower Stretch — late                     ] │
│                                                             │
│ Existing pins on days 43–50 stay with this phase.           │
│ Pins on days 51–56 attach to the new phase. (4 affected.)   │
│                                                             │
│                                       [Cancel]   [Split]    │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Component breakdown

| Component | File | Props (key) | Responsibility | State owned | State read | Query keys / mutations |
|---|---|---|---|---|---|---|
| `<PlannerRecipeListPage>` | `frontend/app/planner/page.tsx` | — | List recipes + "New" button; rewrite of today's `/planner` page | local: search/filter strings; `newRecipeDialogOpen` | recipe list | `queryKeys.growRecipes` |
| `<PlannerPage>` | `frontend/app/planner/recipes/[recipeId]/page.tsx` | (route param `recipeId`) | Page shell; resolves recipeId, mounts `<PlannerCanvas>` inside `<RecipeDraftProvider>` | none | route param | `queryKeys.growRecipe(id)`, `queryKeys.recipeOverrides(id)` |
| `<RecipeDraftProvider>` | `frontend/components/planner/recipe-draft-context.tsx` | `recipe`, `overrides`, `children` | Owns the `useReducer` draft + undo stack; exposes `useRecipeDraft()` context hook | draft recipe, draft overrides, undo stack, redo stack, savedSnapshot | server-loaded recipe + overrides (to seed) | — |
| `<PlannerCanvas>` | `frontend/components/planner/planner-canvas.tsx` | — | Composes header + banner + timeline + side-panel + save-bar; reads draft from context | layout selection (`{kind:'phase',phaseIndex}` \| `{kind:'day',day}` \| `null`) | draft, validation findings | — |
| `<RecipeHeader>` | `frontend/components/planner/recipe-header.tsx` | `name`, `cycleDayCount`, `phaseCount`, `paramCount`, `overrideCount`, `onRename`, `actions` | Name (inline-editable), cycle stats, action slot — uses existing `<PageHeader>` underneath | inline-edit transient state | draft summary | dispatches `RENAME_RECIPE` |
| `<ValidationBanner>` | `frontend/components/planner/validation-banner.tsx` | `findings`, `onFindingClick` | Counts hard/warn findings; expandable list; click finding → calls `onFindingClick(finding)` (planner highlights/scrolls the offending phase or day) | expanded boolean | findings | — |
| `<PhaseTimeline>` | `frontend/components/planner/phase-timeline.tsx` | `phases`, `cycleDayCount`, `overridesByDay`, `selected`, `onSelect` | The horizontal ribbon — renders the day axis, every `<PhaseBlock>`, every `<OverridePinMarker>`, the inter-phase `<PhaseBoundaryHandle>`s. Owns horizontal scroll and pinch-to-zoom (CSS `transform: scaleX()` on a wrapper; min 4 px/day, max 28 px/day). | `pixelsPerDay` (zoom), scroll offset | draft phases + override index | — |
| `<PhaseBlock>` | `frontend/components/planner/phase-block.tsx` | `phase`, `pixelsPerDay`, `colour`, `isSelected`, `onClick` | Renders one coloured block; click → select phase | hover | — | — |
| `<PhaseBoundaryHandle>` | `frontend/components/planner/phase-boundary-handle.tsx` | `leftPhaseIndex`, `rightPhaseIndex`, `boundaryDay`, `pixelsPerDay`, `cycleDayCount` | The grabbable vertical bar between two phases; drag → live preview `MOVE_BOUNDARY_PREVIEW`, drop → commit `MOVE_BOUNDARY`. Constrains `boundaryDay` to `[leftPhase.startDay, rightPhase.endDay - 1]`. Implemented with native pointer events (no library — see "no new heavy dependencies"). | drag state, dragPreviewDay | left + right phase boundaries | dispatch `MOVE_BOUNDARY_PREVIEW`/`MOVE_BOUNDARY` |
| `<OverridePinMarker>` | `frontend/components/planner/override-pin-marker.tsx` | `day`, `paramCategory`, `count`, `pixelsPerDay`, `onClick` | One small shape (circle/triangle/square/diamond by category) above the day. Click → select day | tooltip hover | — | — |
| `<PhaseEditor>` | `frontend/components/planner/phase-editor.tsx` | `phase`, `phaseIndex` | The selected-phase form; renames the phase, edits duration (alternative to drag), light cycle, env band, and the per-param table; "Split phase…" + "Delete" actions at the bottom | local input drafts (debounced 250ms before dispatch) | one phase from draft | dispatch `RENAME_PHASE`, `SET_PHASE_DURATION`, `SET_PHASE_LIGHT_CYCLE`, `SET_PHASE_ENV_BAND`, `OPEN_SPLIT_DIALOG`, `DELETE_PHASE` |
| `<PhaseTargetRow>` | `frontend/components/planner/phase-target-row.tsx` | `phaseIndex`, `paramName`, `target`, `onRemove` | One row in the phase's params table: param name, value, tolerance, unit | local input drafts (debounced) | one target from draft | dispatch `SET_PHASE_TARGET`, `REMOVE_PHASE_TARGET` |
| `<AddPhaseTargetPicker>` | `frontend/components/planner/add-phase-target-picker.tsx` | `phaseIndex`, `existingParamNames` | Dropdown of known param names (from `PARAM_CATALOG`) + "+ Custom param…" escape hatch → opens a tiny inline form | open boolean | — | dispatch `ADD_PHASE_TARGET` |
| `<DayInspector>` | `frontend/components/planner/day-inspector.tsx` | `day` | Resolves every cell for the day (in-browser via the same merge contract as the backend resolver — see `lib/planner/resolver.ts`) and renders one `<DayParamRow>` per resolved cell; "+ Pin parameter…" button at the bottom | none | draft + computed resolution for this day | — |
| `<DayParamRow>` | `frontend/components/planner/day-param-row.tsx` | `day`, `paramName`, `effective`, `phaseDefault` | One row: value (inline-editable when pinned), tolerance, unit, `[default]` / `[PINNED]` badge, edit-pin / drop-pin actions | inline-edit drafts (debounced) | — | dispatch `SET_OVERRIDE`, `REMOVE_OVERRIDE`, `PROMOTE_DEFAULT_TO_PIN` (for editing a default-sourced cell, which creates a pin) |
| `<AddPinDialog>` | `frontend/components/planner/add-pin-dialog.tsx` | `day`, `phaseDefaults`, `open`, `onClose` | Pick a param (from those declared by the day's phase + the `PARAM_CATALOG`), set value/tolerance/unit, confirm → `SET_OVERRIDE` | local form | day's phase | dispatch `SET_OVERRIDE` |
| `<SplitPhaseDialog>` | `frontend/components/planner/split-phase-dialog.tsx` | `phaseIndex`, `open`, `onClose` | Pick the split day; preview both sides + how many pins move to the new phase; confirm → `SPLIT_PHASE` | local form | one phase from draft | dispatch `SPLIT_PHASE` |
| `<NewRecipeDialog>` | `frontend/components/planner/new-recipe-dialog.tsx` | `open`, `onClose` | Pick blank or preset (`cannabis_12week` initially); confirm → POST `/grow-recipes` with the preset shape, then route to the new id | local form | — | mutation `createGrowRecipe`; invalidates `queryKeys.growRecipes` |
| `<DiscardChangesDialog>` | `frontend/components/planner/discard-changes-dialog.tsx` | `open`, `dirtyCount`, `onConfirm`, `onClose` | Confirm → dispatch `RESET_TO_SAVED` | — | dirty count | — |
| `<RecipeSaveBar>` | `frontend/components/planner/recipe-save-bar.tsx` | — | Sticky footer; "N unsaved", undo button, discard button, save button. Triggers the two-PUT save flow (see §5). | local: `saveState` ('idle'\|'saving'\|'partial'\|'ok') | dirty count, validation hard count | mutation `saveRecipeAndOverrides`; invalidates `queryKeys.growRecipe(id)`, `queryKeys.recipeOverrides(id)`, `queryKeys.effectiveTargets(id)`, `queryKeys.growRecipes` |

Supporting modules:

- `frontend/lib/planner/draft-reducer.ts` — the `useReducer` reducer + action type union (`Action`); pure, fully unit-testable.
- `frontend/lib/planner/resolver.ts` — a TypeScript port of `backend/app/core/recipe_resolver.py`. Same merge contract. The day inspector calls this client-side so it doesn't round-trip on every selection.
- `frontend/lib/planner/validator.ts` — TypeScript port of `validate_recipe`; runs on every reducer state and the result is memoised. Output shape: `{ hardRefusals: Finding[]; warnings: Finding[] }` (mirrors `WizardValidationResult`).
- `frontend/lib/planner/param-catalog.ts` — the canonical 11-param set from `cannabis_12week.py` (`REQUIRED_PARAMS`), each tagged with a `category: 'env'|'light'|'irrigation'|'nutrient'|'other'`, default unit, and a "shape" for the pin marker. Used for the `+ Add parameter` dropdown and to colour/shape pins.
- `frontend/lib/planner/colours.ts` — phase colour palette by canonical stage keyword (see §7).
- `frontend/lib/api-client.ts` — extend with `getGrowRecipe`, `listGrowRecipes`, `createGrowRecipe`, `updateGrowRecipe`, `deleteGrowRecipe`, `getEffectiveTargets`, `replaceDayOverrides`, `listDayOverrides`. (Backend currently exposes effective-targets and bulk-replace; the planner needs the basic CRUD wrappers too.)
- `frontend/lib/types.ts` — add `GrowRecipe`, `GrowRecipePhase`, `PhaseTargetSpec`, `EnvironmentalTargets`, `LightCycle`, `DayOverride`, `EffectiveTarget` TS shapes mirroring the Pydantic models (camelCase wire — match the existing pattern in the file).
- `frontend/lib/query-keys.ts` — add `growRecipes`, `growRecipe(id)`, `recipeOverrides(id)`, `effectiveTargets(id, day?)`.

---

## 5. State management

### Draft shape

```ts
type DraftRecipe = GrowRecipe;          // mutable copy (with phases re-derived after every edit)
type DraftOverride = DayOverride;       // {day, paramName, value, tolerance?, unit?}
type DraftState = {
  recipe: DraftRecipe;
  overrides: DraftOverride[];
  savedSnapshot: { recipe: DraftRecipe; overrides: DraftOverride[] };  // last server-confirmed state
  undoStack: Snapshot[];                // max 50; pushed on every action that mutates recipe/overrides
  redoStack: Snapshot[];                // cleared on every non-undo/redo action
};
```

### Update strategy — batched, not optimistic

Pass-5b's `<SensorRolePicker>` went per-pick because each pick is
independent and a failure on one doesn't poison the others. Recipe edits
**are** interdependent:

- Moving the `Late Veg → Flower Stretch` boundary from d42→43 to d44→45
  changes which days belong to each phase, which changes the resolved
  targets of d42-44, which changes which override rows are orphaned,
  which changes the validator output.
- A pure-optimistic per-edit save would either (a) thrash the server
  with N+1 PUTs that mostly succeed, or (b) require a hand-rolled queue
  and request-coalescing that re-invents the batched flow.

The recipe is therefore a **batched draft**. Explicit Save / Discard.
Validation runs client-side on every reducer step so the operator sees
findings instantly without paying RTT.

### Where the draft lives

`useReducer` inside `<RecipeDraftProvider>`, exposed via a small
`useRecipeDraft()` context hook (returns `{state, dispatch, undo, redo,
save, discard, dirtyCount, findings}`). No Zustand — one provider in one
route subtree is exactly what context+reducer is for, and adding Zustand
for one screen is overkill given the existing patterns in the codebase.

### Reducer actions

```
RENAME_RECIPE       { name }
RENAME_PHASE        { phaseIndex, name }
SET_PHASE_DURATION  { phaseIndex, days }      // re-stamps start/end days for this + all subsequent
SET_PHASE_LIGHT_CYCLE   { phaseIndex, hoursOn, hoursOff }
SET_PHASE_ENV_BAND  { phaseIndex, key, min, max }
SET_PHASE_TARGET    { phaseIndex, paramName, value, tolerance, unit }
ADD_PHASE_TARGET    { phaseIndex, paramName, value, tolerance, unit }
REMOVE_PHASE_TARGET { phaseIndex, paramName }
SPLIT_PHASE         { phaseIndex, atDay, newPhaseName }   // creates a new phase after this one
DELETE_PHASE        { phaseIndex }            // merges this phase's days into the previous phase
MOVE_BOUNDARY_PREVIEW   { leftIndex, day }    // live preview only, NOT pushed to undo stack
MOVE_BOUNDARY       { leftIndex, day }        // commits the boundary; pushes undo
ADD_PHASE_BEFORE    { phaseIndex, name, days }
ADD_PHASE_AFTER     { phaseIndex, name, days }
SET_OVERRIDE        { day, paramName, value, tolerance, unit }  // upsert
REMOVE_OVERRIDE     { day, paramName }
UNDO                {}
REDO                {}
RESET_TO_SAVED      {}
LOAD_FROM_SERVER    { recipe, overrides }     // dispatched once after the load query lands
```

Every mutating action pushes the current state onto `undoStack` (capped
at 50) and clears `redoStack`. After every action, the reducer re-runs
the "stamp start/end days" logic from
`GrowRecipeBase._populate_phase_day_boundaries` (TS port) so phases
always carry derived day boundaries. After every action, `validator.ts`
re-runs over the new state and the memoised findings are exposed via
context.

### Undo / redo

Stack of full state snapshots. 50 entries is plenty for a planning
session; deep-clone via `structuredClone` (built-in, no library). Live
drag-preview actions (`MOVE_BOUNDARY_PREVIEW`) bypass the undo stack —
only the final committed `MOVE_BOUNDARY` is undoable. Cmd/Ctrl+Z and
Cmd/Ctrl+Shift+Z bound globally inside the canvas.

### Dirty detection

`dirtyCount = countDifferentCells(state.recipe, savedSnapshot.recipe) +
countDifferentOverrides(state.overrides, savedSnapshot.overrides)`.
Cheap because both shapes are flat. Exposed via context.

### Save flow

Save is enabled only when `dirtyCount > 0 && findings.hardRefusals.length === 0`. On click:

1. `saveState = 'saving'`.
2. PUT `/api/cultivation/grow-recipes/{id}` with the recipe body
   (camelCase via `model_dump(by_alias=True)`-shaped object).
3. On success, PUT `/api/cultivation/grow-recipes/{id}/day-overrides`
   with `{ overrides: [...] }`.
4. On both-success: invalidate `queryKeys.growRecipe(id)`,
   `queryKeys.recipeOverrides(id)`, `queryKeys.effectiveTargets(id)`,
   `queryKeys.growRecipes`. Then dispatch `LOAD_FROM_SERVER` with the
   response so `savedSnapshot` resets. Clear undo stack.
   `saveState = 'ok'`.
5. On step-2 failure: show error, `saveState = 'idle'`, draft stays
   dirty (no server mutation happened).
6. On step-3 failure: show **partial-failure** error in the save bar —
   "Recipe header saved (v2) but overrides failed: <detail>. Retry
   overrides or discard." `saveState = 'partial'`. Provide an explicit
   "Retry overrides" button that re-runs step 3 only. This is the
   realistic failure mode (the bulk-replace endpoint validates more
   aggressively than the recipe PUT). Don't try to roll back the recipe
   PUT — it's a valid recipe regardless.

The two-step is acceptable because step-3 is itself atomic on the
server (DELETE-then-INSERT in one transaction), so the worst observable
inconsistency is "recipe phases updated but overrides still the old
set" — a state the operator can fix by either retrying or by editing
overrides to match.

### Cache invalidation

Single source of truth: after a successful save, invalidate:
- `queryKeys.growRecipe(id)` — header + phases.
- `queryKeys.recipeOverrides(id)` — override list.
- `queryKeys.effectiveTargets(id)` — the resolved grid (the dashboard
  and the AI supervisor read this; it must reflect the new state).
- `queryKeys.growRecipes` — the recipe list (the updated-at timestamp).

After a successful new-recipe create, additionally invalidate
`queryKeys.growRecipes` (list) before navigating.

---

## 6. Workflows

### W1. Open an existing recipe

1. Operator hits `/planner` → recipe list (queryKey `growRecipes`).
2. Clicks a recipe → routes to `/planner/recipes/{id}`.
3. `<PlannerPage>` fires `growRecipe(id)` + `recipeOverrides(id)`
   queries in parallel.
4. Both succeed → `<RecipeDraftProvider>` mounts, dispatches
   `LOAD_FROM_SERVER`, sets `savedSnapshot`.
5. `<PhaseTimeline>` renders; no phase or day selected; right panel says
   "Click a phase to edit or a day to inspect."

### W2. Edit a phase default

1. Operator clicks the `Late Veg` block.
2. `<PhaseEditor>` mounts in the right panel, pre-populated.
3. Operator changes `temp_day` value from 24 to 25 in
   `<PhaseTargetRow>`.
4. 250ms after the last keystroke, `SET_PHASE_TARGET` dispatches.
5. `dirtyCount` ticks up, save bar shows "1 unsaved",
   `<RecipeSaveBar>` Save button enables (assuming no hard findings).
6. Operator clicks Save → save flow §5.

### W3. Add a per-day pin

1. Operator clicks day 49 on the day axis of `<PhaseTimeline>`.
2. `<DayInspector>` mounts in the right panel for day 49, resolves every
   cell via `lib/planner/resolver.ts`.
3. Operator clicks `[+ Pin another parameter…]` → `<AddPinDialog>`
   opens.
4. Picks `rh_day` from the dropdown (defaults to
   `phaseDefaults.rh_day` for value/tol/unit if present, else
   `PARAM_CATALOG` defaults).
5. Changes value 55→52, clicks `Pin`.
6. Dispatches `SET_OVERRIDE`. Day inspector re-renders with the new pin
   (badge flips to `[PINNED]`). Timeline shows a new pin marker above
   day 49.
7. Save → save flow.

### W4. Remove a pin

1. With day 49 selected and pin visible on `rh_day`, operator clicks the
   `[×]` next to the pin badge.
2. Dispatches `REMOVE_OVERRIDE { day:49, paramName:'rh_day' }`.
3. Day inspector re-renders; row now shows `[default]` with the phase
   default value (55).
4. Pin marker on the timeline disappears (or shrinks if other params on
   day 49 still pinned).
5. Save → save flow.

### W5. Split a phase

1. Operator selects `Flower Stretch` (d43-56).
2. Clicks `[Split phase here…]` at the bottom of `<PhaseEditor>`.
3. `<SplitPhaseDialog>` opens with a numeric input pre-filled to the
   midpoint (d49).
4. Operator types 50, types a new phase name "Flower Stretch — late".
   Preview updates: left d43-50 (8d), right d51-56 (6d). Affected-pins
   counter: "4 pins will attach to the new phase."
5. Confirms → dispatches `SPLIT_PHASE { phaseIndex, atDay:50,
   newPhaseName }`. The reducer:
   - shrinks the original phase to `durationDays = 8`,
   - inserts a new phase after it with `durationDays = 6`,
     `order = original.order + 0.5` (re-normalised to integer orders
     by the boundary-stamp pass), copy of the original's `targets`,
     `lightCycle`, `environmentalTargets`, `nutrients`.
   - reshuffles `order` values to be consecutive integers across all
     phases.
6. Save → save flow.

### W6. Move a phase boundary

1. Operator presses-and-holds the boundary handle between `Late Veg`
   and `Flower Stretch` (currently between d42 and d43).
2. Dragging fires `MOVE_BOUNDARY_PREVIEW { leftIndex:1, day:<rolling> }`
   on every pointermove (16ms throttle). The preview action mutates the
   draft transiently but is excluded from the undo stack; the timeline
   re-renders the new boundary every frame; the right panel and findings
   count are also re-derived live so the operator sees the cost of the
   drag as they drag.
3. Pointer release at d45 → fires `MOVE_BOUNDARY { leftIndex:1,
   day:45 }`. The committed action is pushed to the undo stack as one
   entry (the preview chain is discarded).
4. The reducer adjusts `Late Veg.durationDays = 24` and
   `Flower Stretch.durationDays = 12`; re-stamps `start_day`/`end_day`
   across all phases.
5. Validator runs; if any override on (former) d43-45 was already an
   orphan for the now-different phase, an `override_param_orphan`
   warning appears in the banner.
6. Save → save flow.

### W7. Reorder phases — **recommendation: implicit only**

Phases derive their order from `order` + `durationDays`. Drag-to-reorder
the entire ribbon is a misleading metaphor — reordering a phase changes
which days it covers, which changes pin meaning, which is hard to
reason about while dragging.

Instead, **explicit `[+ Phase before]` / `[+ Phase after]`** buttons in
the timeline, plus `[Delete]` in the phase editor (deleting merges the
phase's days into the previous phase, growing its `durationDays`). This
is the only reorder-equivalent the planner offers. The Convex `order`
field is normalised to a dense 1..N sequence by the reducer after every
structural action, so the wire format always satisfies the
unique-order constraint.

### W8. Create a new recipe from a preset

1. From `/planner` recipe list, operator clicks `[+ New recipe]`.
2. `<NewRecipeDialog>` opens. Pick from `Blank` or `cannabis_12week`.
   (Future: more presets — list pulled from
   `GET /api/cultivation/grow-recipes/presets` once that endpoint
   exists; pass 1 hard-codes the one preset.)
3. Operator enters a name "Blue Dream — F2", picks `cannabis_12week`,
   optionally picks a genetics from a dropdown.
4. The dialog builds the GrowRecipe body client-side from a static
   preset definition (TypeScript port of `cannabis_12week.py` lives in
   `frontend/lib/planner/presets/cannabis-12week.ts` — same phases,
   same per-day overrides for the legacy interpolation). POSTs
   `/api/cultivation/grow-recipes`, then PUTs
   `/api/cultivation/grow-recipes/{id}/day-overrides` with the override
   set.
5. On both-success, route to `/planner/recipes/{newId}`. Pre-loaded
   state matches the preset.

(Alternative: surface the preset registry as a backend endpoint and
have the dialog POST `{ presetName, recipeName }` to a new
`POST /grow-recipes/from-preset` route. Cleaner long-term but adds
backend scope. Pass 1 does the client-side bake; mention this as Open
Question Q3.)

### W9. Resolve a validation finding

1. Banner shows "1 hard finding". Operator clicks `[View all]`.
2. Banner expands to a list. Each finding row is clickable and carries
   a target hint: `phaseIndex` and/or `day` and/or `paramName`.
3. Operator clicks "Phase 'Flower Stretch' has no targets — every day
   will need an override row…"
4. `<PlannerCanvas>` sets selection to
   `{kind:'phase', phaseIndex:2}` and scrolls the timeline so the phase
   is centred. The phase editor opens. Param table is empty; an
   empty-state row says "No defaults — click + Add parameter to declare
   one."
5. Operator adds parameters; warning clears.

### W10. Discard unsaved changes

1. Operator has 5 unsaved edits, clicks `[Discard…]` in the save bar.
2. `<DiscardChangesDialog>` confirms ("Discard 5 unsaved edits?").
3. Confirm → dispatch `RESET_TO_SAVED`. Reducer rewinds `recipe` +
   `overrides` to `savedSnapshot`, clears undo/redo, `dirtyCount → 0`.
4. Save button disables; banner re-runs validator on the saved state.

(Bonus: a `beforeunload` listener fires when `dirtyCount > 0` so the
operator gets the browser's "leave this page?" prompt on navigation
away.)

---

## 7. Visual treatment

### Phase colour palette (Tailwind named colours, dark-mode tuned)

Phases are coloured by **canonical stage**, inferred from `phase_name`
via a tiny case-insensitive matcher in `lib/planner/colours.ts`. The
match falls through to a neutral grey when no keyword matches.

| Canonical stage | Keywords | Tailwind class (bg/border) |
|---|---|---|
| Propagation (clone/seedling) | `propag`, `clone`, `seedling`, `mother` | `bg-emerald-700/30 border-emerald-500` |
| Veg (early) | `early veg`, `veg 1` | `bg-green-700/30 border-green-500` |
| Veg (late) | `late veg`, `veg 2`, `bulking` | `bg-lime-700/30 border-lime-500` |
| Stretch | `stretch`, `transition` | `bg-amber-700/30 border-amber-500` |
| Flower (bulk) | `flower`, `bloom`, `bulk` | `bg-orange-700/30 border-orange-500` |
| Ripening | `ripen`, `swell` | `bg-red-700/30 border-red-500` |
| Flush | `flush` | `bg-rose-900/30 border-rose-500` |
| (fallback) | — | `bg-zinc-700/30 border-zinc-500` |

The phase block uses the bg class for fill and the border class for the
left/right edges; the label text is `text-foreground` for legibility,
not the stage colour.

### Override-pin palette + shapes (categories from `PARAM_CATALOG`)

| Category | Params | Shape | Tailwind |
|---|---|---|---|
| env | `temp_day`, `temp_night`, `rh_day`, `rh_night`, `co2_day`, `vpd_day` | circle | `bg-sky-400` |
| light | `ppfd`, `photoperiod_hours`, `dli` | triangle | `bg-yellow-400` |
| irrigation | `vwc_target`, `dryback_pct` | square | `bg-blue-400` |
| nutrient | `ec_target`, `ph_target` | diamond | `bg-violet-400` |
| other | (custom params) | hollow circle | `border-zinc-300 bg-transparent` |

When multiple categories are pinned on the same day, render up to 3
small markers stacked vertically above the day, then a `+N` badge when
there are more. The full pin list appears on hover (HTML `title`
attribute is fine — no need for a tooltip lib).

### Empty states

- **No recipes** — recipe list shows `<Inbox/>` + "No recipes yet.
  Create one from a preset to start planning." with a `[+ New recipe]`
  button.
- **Recipe with no phases** — actually unreachable (`min_length=1` on
  the schema), but the planner defensively shows "This recipe has no
  phases. Open in JSON view to repair." with a link to the raw JSON.
- **Phase with no params** — phase editor's params table shows "No
  phase defaults — every day in this phase will need a pin to have an
  effective target." with `[+ Add parameter ▼]` underneath. Validator
  surfaces this as a warning.

### Loading + error states

- Recipe + overrides loading → `<QueryState isLoading>` covers the
  whole canvas (matches the existing pattern).
- Recipe 404 → `<QueryState notFoundMessage="Recipe not found — it may
  have been deleted.">` with a `[Back to recipes]` link.
- Save mutation pending → save bar Save button shows spinner +
  "Saving…", everything else stays interactive (so the operator can
  keep navigating but not dispatch another save).
- Save mutation error → save bar grows an error row underneath
  (matching the existing `text-critical` pattern in `<QueryState>`).

---

## 8. Accessibility

### Keyboard

- **Tab order**: header → validation banner (if findings) → timeline
  (single focusable region, then arrow keys take over) → phase editor
  or day inspector → save bar.
- **Inside the timeline**: arrow-left/arrow-right moves the day cursor
  by 1 day; shift+arrow moves by 7 days; home/end jump to day 1 /
  `cycleDayCount`. The currently-focused day has a faint vertical
  highlight line.
- **`Space` / `Enter`** on a focused day → opens the day inspector.
  **`Enter`** on a focused phase block → opens the phase editor.
- **`P`** (when a day is focused) → opens `<AddPinDialog>` for that
  day.
- **`S`** (when a phase is focused) → opens `<SplitPhaseDialog>` for
  that phase.
- **`Cmd/Ctrl+Z`** / **`Cmd/Ctrl+Shift+Z`** → undo/redo.
- **`Cmd/Ctrl+S`** → save (no-op when disabled).
- **`Esc`** → close dialog if open, else clear selection.

### Screen reader

- Phase blocks: `aria-label="Phase Late Veg, days 22 to 42, 21 days.
  Click to edit."` and `role="button"`.
- Pin markers: `aria-label="Override pin on day 49 for rh_day. Click
  to inspect day."`.
- Day axis: rendered as a `<div role="slider" aria-valuemin=1
  aria-valuemax=84 aria-valuenow={focusedDay} aria-label="Day
  cursor">` so AT exposes the day-stepping affordance.
- Validation banner: `<div role="status" aria-live="polite">` so
  finding changes announce.
- Save bar: dirty count + save state announced via the same polite
  live region.

### Colour-blind

Pin markers are distinguished by **both** colour and shape (see §7),
so a deuteranope can tell env from irrigation by the circle-vs-square.
Phase colours are different enough in saturation but the planner
additionally writes the phase name and day range *inside* each block —
colour is decoration, the textual identification is primary.

---

## 9. Implementation phases

| Pass | Scope | New / changed files | Depends on | Verification |
|---|---|---|---|---|
| **P1: Page shell + recipe list + read-only timeline** | Rewrite `/planner` page as a recipe list; new `/planner/recipes/[id]` page that loads the recipe + overrides and renders a read-only `<PhaseTimeline>` (no editing, no side panel). | `frontend/app/planner/page.tsx` (rewrite), `frontend/app/planner/recipes/[recipeId]/page.tsx`, `frontend/app/planner/recipes/[recipeId]/planner-page.tsx`, `frontend/components/planner/phase-timeline.tsx`, `frontend/components/planner/phase-block.tsx`, `frontend/components/planner/override-pin-marker.tsx`, `frontend/lib/types.ts` (add new shapes), `frontend/lib/query-keys.ts` (add new keys), `frontend/lib/api-client.ts` (add wrappers), `frontend/lib/planner/colours.ts`, `frontend/lib/planner/param-catalog.ts` | none | Visit the planner, see existing recipes, see their phase ribbon with correct phase widths and override pins. No edits possible. Read-only. |
| **P2: Draft reducer + phase editor + Save** | Add `<RecipeDraftProvider>` with the reducer, `<PhaseEditor>` (rename + duration + light cycle + env band + targets table), `<RecipeSaveBar>` with the two-PUT save flow, `<DiscardChangesDialog>`. No drag yet — boundary moves via the duration input only. | `frontend/components/planner/recipe-draft-context.tsx`, `frontend/lib/planner/draft-reducer.ts`, `frontend/lib/planner/validator.ts`, `frontend/components/planner/phase-editor.tsx`, `frontend/components/planner/phase-target-row.tsx`, `frontend/components/planner/add-phase-target-picker.tsx`, `frontend/components/planner/recipe-save-bar.tsx`, `frontend/components/planner/discard-changes-dialog.tsx`, `frontend/components/planner/validation-banner.tsx` | P1 | Open a recipe, change a phase name and a temp value, save, reload — changes persist. Discard restores. Banner shows seeded findings when present. |
| **P3: Day inspector + pin editing** | Add `<DayInspector>`, `<DayParamRow>`, `<AddPinDialog>`. Port resolver to TS. Pin markers in the timeline become clickable and selecting a day mounts the inspector. | `frontend/components/planner/day-inspector.tsx`, `frontend/components/planner/day-param-row.tsx`, `frontend/components/planner/add-pin-dialog.tsx`, `frontend/lib/planner/resolver.ts` | P2 | Click a day, see resolved cells, add a pin, see it appear, remove it, save, reload — persists. Resolver output matches `GET /effective-targets?day=N`. |
| **P4: Phase split + delete + drag-to-resize boundary** | `<SplitPhaseDialog>`, `<PhaseBoundaryHandle>` with native pointer events (no library), delete-phase action. | `frontend/components/planner/split-phase-dialog.tsx`, `frontend/components/planner/phase-boundary-handle.tsx`, reducer updates for `SPLIT_PHASE`, `MOVE_BOUNDARY`, `MOVE_BOUNDARY_PREVIEW`, `DELETE_PHASE` | P3 | Drag a boundary, see phase widths update live, drop, save, reload — persists. Split phase via the dialog, save, reload — persists. Orphan-pin warning fires when expected. |
| **P5: Undo/redo + new-from-preset + polish** | Wire `Cmd/Ctrl+Z` keybinds, undo/redo buttons in the save bar, `<NewRecipeDialog>` with the TS-side `cannabis_12week` preset, keyboard navigation on the timeline (arrow keys + space), accessibility audit (aria labels, focus rings), final visual polish on pin markers (multi-stack), and an empty-state for the recipe list. | `frontend/components/planner/new-recipe-dialog.tsx`, `frontend/lib/planner/presets/cannabis-12week.ts`, keyboard wiring in `phase-timeline.tsx`, hotkey hook | P4 | Create a new recipe from preset and immediately edit. Undo/redo a full editing session. Arrow-key navigate days. Lighthouse a11y score ≥ 95 on the planner page. |

Each pass is a single PR; passes 1-3 are roughly equal in size, pass 4 is the heaviest (drag mechanics), pass 5 is the smallest.

---

## 10. Open questions

| # | Question | Recommended default |
|---|---|---|
| Q1 | Drag-to-resize boundary **vs** explicit duration-input — which is the primary input? | **Both, with duration as the canonical truth.** Drag is the headline interaction; the duration input in `<PhaseEditor>` is the keyboard/a11y path and the exact-number-needed path. They dispatch the same action under the hood. |
| Q2 | Should the canonical param-name set be open or constrained? | **Soft-constrained.** Ship `PARAM_CATALOG` (the 11 from `cannabis_12week`) as a typeahead with categories + units + pin shape pre-populated, but allow `+ Custom param…` for power users. Custom params get the "other" shape and unit free-text. Don't enforce server-side — `param_name: str` in the schema already allows anything. |
| Q3 | Preset creation: client-bake the cannabis preset in TS, or add a `POST /grow-recipes/from-preset` endpoint? | **P5 client-bake**; backend endpoint in a follow-up pass. Reason: avoids backend coupling for a single preset; the TS shape mirrors `cannabis_12week.py` directly. Revisit when there are 3+ presets. |
| Q4 | Should pinning a value equal to the phase default auto-drop the pin? | **No, but show a "matches default" hint.** Auto-dropping is confusing — the operator may have intended to "freeze" that day's value so future phase-default edits don't propagate to it. Surface a subtle `(same as default)` label and offer a one-click `[Drop pin]` next to it. |
| Q5 | Does the planner show the underlying batch/room a recipe is assigned to? | **Show a "Used by" badge** at the top — "Used by batch BD-2025-W14 in F2". Click → goes to that batch. Pulled from `GET /api/cultivation/batches?growRecipe={id}`. P5 polish, not blocking. |
| Q6 | Cycle length input — exposed directly, or always derived from phase durations? | **Always derived.** Don't expose `estimatedTotalDurationDays` as an editable field — the schema's prefix-sum is the truth. Show "84-day cycle" as a computed badge in `<RecipeHeader>`. |
| Q7 | Single phase that covers all days (a freshly-migrated recipe per the migration tool) — show a special CTA? | **Yes.** When `phases.length === 1` and `phases[0].targets` is empty, show a top-of-canvas helper card: "This recipe came from the legacy day-grid. Click 'Split phase here' to start carving it into real cultivation phases." Dismissable. |
| Q8 | Permissioning — is the planner admin-only? | **Yes, follow existing pattern** (`require_role(admin)` on every grow-recipe route). The frontend trusts the backend's 403; no client-side role gate needed beyond what already exists. |

---

## 11. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Validator + resolver re-run on every keystroke** (the recipe is ~5 phases × 11 params + ~50 overrides = small, but a stretched zoom + frequent drag = 60 fps). | Memoise the validator output on a content hash of `(recipe, overrides)`. Throttle `MOVE_BOUNDARY_PREVIEW` to 16ms. The resolver only runs for the *currently selected day* in `<DayInspector>` (not the full grid), so it's bounded to ~11 cells. |
| R2 | **Drag interaction on touch devices** (tablets are plausible — the appliance ships with a touch panel option). | Use pointer events (not mouse events) — they unify mouse, pen, and touch. Increase the boundary-handle hit zone to 24×24 px (visible width 4 px). Test on a real touch device in P4. |
| R3 | **Partial save failure** — recipe PUT succeeds, override PUT fails, leaving operator confused. | Explicit `saveState='partial'` in the save bar with a "Retry overrides" button (not a blind re-save). Audit-log on the backend already records each PUT separately so the trail is unambiguous. |
| R4 | **Orphan-pin proliferation** — moving boundaries silently leaves pins on days that no longer belong to their original phase, with mismatched param. | Validator surfaces `override_param_orphan` as a warning the moment it happens. The day inspector also flags the row with an `[orphan]` badge. Add a one-click "Drop all orphan pins" action in the validation banner's expanded view. |
| R5 | **Concurrent edits** — two operators open the same recipe in two browser tabs, both save. Whoever saves last wins, no merge. | Document the limitation in the empty-state copy ("Only one editor at a time — last save wins"). A real solve needs ETag-style optimistic concurrency on the backend (`If-Match` header + 412 response), which is out of scope for the planner pass. Track as a follow-up issue. |
