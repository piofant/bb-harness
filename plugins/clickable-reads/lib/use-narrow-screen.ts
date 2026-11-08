import { useEffect, useState } from "react";

/** Tailwind's `max-sm` query, so wrapped code and icon-only buttons switch together. */
export const NARROW_SCREEN_QUERY = "not all and (min-width: 40rem)";

/** Phones get wrapped code lines: sideways scrolling is awkward there. */
export function useNarrowScreen(): boolean {
  const [narrow, setNarrow] = useState(
    () => typeof window.matchMedia === "function" && window.matchMedia(NARROW_SCREEN_QUERY).matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(NARROW_SCREEN_QUERY);
    const onChange = () => setNarrow(media.matches);
    media.addEventListener("change", onChange);
    // A change between the first render and subscribing would be missed otherwise.
    onChange();
    return () => media.removeEventListener("change", onChange);
  }, []);
  return narrow;
}
