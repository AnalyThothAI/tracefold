import type { WindowKey } from "@lib/types";
import { ToggleGroup, ToggleGroupItem } from "@shared/ui/toggle-group";

import type { SearchRouteState } from "../state/searchRouteState";

const WINDOW_OPTIONS: WindowKey[] = ["5m", "1h", "4h", "24h"];

type SearchIntelControlsProps = {
  routeState: SearchRouteState;
  onRouteChange: (patch: Partial<SearchRouteState>) => void;
};

export function SearchIntelControls({ routeState, onRouteChange }: SearchIntelControlsProps) {
  const handleWindowChange = (nextWindow: string) => {
    if (!nextWindow) {
      return;
    }
    if (!WINDOW_OPTIONS.includes(nextWindow as WindowKey)) {
      return;
    }
    onRouteChange({ window: nextWindow as WindowKey });
  };

  return (
    <div className="search-intel-controls" aria-label="Search Intel controls">
      <section>
        <span>window</span>
        <ToggleGroup
          aria-label="search window"
          className="search-segmented"
          onValueChange={handleWindowChange}
          type="single"
          value={routeState.window}
        >
          {WINDOW_OPTIONS.map((window) => (
            <ToggleGroupItem className="search-segmented-item" key={window} value={window}>
              {window}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </section>
    </div>
  );
}
