import { useEffect, useState } from "react";

/**
 * Which layout is on screen, read synchronously so the first paint is already correct: mounting a sidebar and
 * then swapping it for a bottom bar, or opening a drawer on a phone, would move the whole page under the
 * reader.
 *
 * This is for structure the browser cannot express in CSS — a component that must not be *mounted* at a
 * width, a control that must not exist under a thumb. Anything that is only a matter of appearance stays a
 * media query in the owner's stylesheet.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof window !== "undefined" && window.matchMedia(query).matches,
  );
  useEffect(() => {
    const media = window.matchMedia(query);
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}
