# Default & matched images for acer-travel-plan

## Policy (required order)

1. **Matched destination photos first (method A).**  
   If the itinerary names concrete places or local foods, you **must try** Wikimedia Commons photos that visually show those places/foods before using any default.
2. **Defaults are last resort, per slot.**  
   Use a default only for the slot whose lookup failed or whose photo match is uncertain. Do **not** fill all highlight slots with defaults when famous places are already named.
3. Prefer `upload.wikimedia.org` **960px** (or full file if no thumb) URLs.  
   Do **not** use other thumb widths such as `800px` / `1200px` — Commons may reject them with HTTP 400.  
   Keep gold gradient CSS as a last visual underlay.  
   The HTML template also has **runtime fallbacks**: if an `<img>` fails to load (`onerror`), it swaps to that slot's default URL below (once). Do **not** remove `data-fallback` / `onerror` from `template.html` when filling a trip.

## Defaults (generic travel atmosphere — not a specific place)

Use these **only** when a matched photo cannot be found for that slot:

| Slot | URL | Mood |
|------|-----|------|
| Highlight 1 (main) | `https://upload.wikimedia.org/wikipedia/commons/thumb/b/b7/View_from_airplane_window.jpg/960px-View_from_airplane_window.jpg` | Flight / clouds |
| Highlight 2 | `https://upload.wikimedia.org/wikipedia/commons/thumb/e/ef/Beach_sunset.jpg/960px-Beach_sunset.jpg` | Horizon / sunset |
| Highlight 3 | `https://upload.wikimedia.org/wikipedia/commons/thumb/4/4b/Empty_road.jpg/960px-Empty_road.jpg` | Open road / journey |
| Meals banner | `https://upload.wikimedia.org/wikipedia/commons/thumb/8/87/Breakfast.jpg/960px-Breakfast.jpg` | Everyday meal table |
| Per-meal side photo (each meal row) | same Breakfast default as above | Fallback only if that dish photo cannot be found |

**Per-meal photos (required with each recommended meal):**  
Each meal list row has text on the left and a **food photo on the right** (`.meal-photo`). Prefer a Commons photo of that dish; if lookup fails, use the meals default for **that row only**. Stays do not use this layout.

**Do not use as defaults:** identifiable temples, towers, city skylines, airport equipment close-ups, wildlife/safari scenes, country-specific passports, or cuisine that pins one country (e.g. sushi-only) unless the trip content matched that photo.

## How to find a matched image (required steps)

1. Take the exact place name from highlights / daily plan (e.g. `Okinawa Churaumi Aquarium`, `Shurijo Castle`, `Osaka Castle`).
2. Search Wikimedia Commons for that place name.
3. Pick a photo that **visually matches** that stop (not a random city skyline, not a different island/city).
4. Resolve via `https://commons.wikimedia.org/wiki/Special:FilePath/<FileName>?width=960` and write the final `upload.wikimedia.org/...` URL into the HTML.
5. Set `alt` to the place/food name.
6. If unsure the photo matches, keep the default for **that highlight/meals slot only**.

## Daily itinerary photos (optional)

Daily photos follow a different rule from highlights/meals:

1. Try a matched Commons photo for that **specific** Morning / Afternoon / Evening stop.
2. If found, put **one** `.slot-photo` before `.place` in that slot only, with:
   `onerror="var p=this.closest('.slot-photo');if(p)p.style.display='none';"`
   so a failed load (offline / 404) hides the box instead of showing a broken-image icon.
3. If **not** found: **omit** the photo markup. Do **not** use the defaults table above for daily slots.
4. Do **not** add a `.day-photos` strip at the top of the day (that duplicates slot photos).
5. Never use unrelated news/event photos that only mention the place name in the filename.

## Examples

| Trip content names | Expected mosaic | Wrong |
|--------------------|-----------------|-------|
| Churaumi Aquarium, Shurijo Castle, Emerald Beach | photos of those places | airplane / sunset / road defaults for all 3 |
| Osaka Castle, Universal Studios Japan, Kaiyukan | photos of those places | defaults when Commons has clear matches |
| Obscure local cafe with no Commons photo | that highlight/meals slot may use default; daily slot omits photo | inventing a random Unsplash URL |
| Daily stop with no Commons match | no `.slot-photo` for that stop | empty gold daily box or day-top album of duplicates |
