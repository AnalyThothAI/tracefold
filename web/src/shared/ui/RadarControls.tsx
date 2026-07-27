import { OBSERVATION_WINDOWS } from "@lib/observationWindows";
import type { WindowKey } from "@lib/types";

import { ToggleGroup, ToggleGroupItem } from "./toggle-group";
import "./RadarControls.css";

type RadarControlsProps = {
  windowKey: WindowKey;
  onWindowChange: (window: WindowKey) => void;
};

export function RadarControls({ windowKey, onWindowChange }: RadarControlsProps) {
  const handleWindowChange = (nextWindow: string) => {
    if (!nextWindow) {
      return;
    }
    if (!OBSERVATION_WINDOWS.includes(nextWindow as WindowKey)) {
      return;
    }
    onWindowChange(nextWindow as WindowKey);
  };

  return (
    <ToggleGroup
      aria-label="radar window"
      className="radar-controls-group radar-controls-window"
      onValueChange={handleWindowChange}
      type="single"
      value={windowKey}
    >
      {OBSERVATION_WINDOWS.map((item) => (
        <ToggleGroupItem className="radar-controls-item" key={item} value={item}>
          {item}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
