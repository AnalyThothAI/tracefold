import type { MacroTypedModuleReadData } from "../model/macroTypes";

import { MacroModuleSections, RatesDecisionSummary } from "./MacroModuleSections";

export function MacroModuleWorkspace({ module }: { module: MacroTypedModuleReadData }) {
  return (
    <>
      {module.module_id === "rates_fed" ? <RatesDecisionSummary module={module} /> : null}
      <MacroModuleSections module={module} />
    </>
  );
}
