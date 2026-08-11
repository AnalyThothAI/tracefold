from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args

from pydantic import TypeAdapter

from tracefold.app.http import schemas as api_schemas
from tracefold.macro import MACRO_MODULE_DEFINITIONS, MACRO_MODULE_IDS, MacroModuleId


@dataclass(frozen=True, slots=True)
class MacroHttpModuleDefinition:
    module_id: MacroModuleId
    api_path: str
    href: str
    persisted_schema: type[api_schemas.ExactApiSchema]
    read_envelope: TypeAdapter[Any]


MACRO_HTTP_MODULES = (
    MacroHttpModuleDefinition(
        "rates_fed",
        "/api/macro/rates-fed",
        "/macro/rates-fed",
        api_schemas.MacroRatesFedPersistedData,
        TypeAdapter(
            api_schemas.ApiEnvelope[api_schemas.MacroRatesFedReadData | api_schemas.MacroModuleUnavailableData]
        ),
    ),
    MacroHttpModuleDefinition(
        "economy_inflation",
        "/api/macro/economy-inflation",
        "/macro/economy-inflation",
        api_schemas.MacroEconomyInflationPersistedData,
        TypeAdapter(
            api_schemas.ApiEnvelope[api_schemas.MacroEconomyInflationReadData | api_schemas.MacroModuleUnavailableData]
        ),
    ),
    MacroHttpModuleDefinition(
        "liquidity_funding",
        "/api/macro/liquidity-funding",
        "/macro/liquidity-funding",
        api_schemas.MacroLiquidityFundingPersistedData,
        TypeAdapter(
            api_schemas.ApiEnvelope[api_schemas.MacroLiquidityFundingReadData | api_schemas.MacroModuleUnavailableData]
        ),
    ),
    MacroHttpModuleDefinition(
        "credit",
        "/api/macro/credit",
        "/macro/credit",
        api_schemas.MacroCreditPersistedData,
        TypeAdapter(api_schemas.ApiEnvelope[api_schemas.MacroCreditReadData | api_schemas.MacroModuleUnavailableData]),
    ),
    MacroHttpModuleDefinition(
        "volatility",
        "/api/macro/volatility",
        "/macro/volatility",
        api_schemas.MacroVolatilityPersistedData,
        TypeAdapter(
            api_schemas.ApiEnvelope[api_schemas.MacroVolatilityReadData | api_schemas.MacroModuleUnavailableData]
        ),
    ),
    MacroHttpModuleDefinition(
        "cross_asset",
        "/api/macro/cross-asset",
        "/macro/cross-asset",
        api_schemas.MacroCrossAssetPersistedData,
        TypeAdapter(
            api_schemas.ApiEnvelope[api_schemas.MacroCrossAssetReadData | api_schemas.MacroModuleUnavailableData]
        ),
    ),
)

if tuple(item.module_id for item in MACRO_HTTP_MODULES) != MACRO_MODULE_IDS:
    raise RuntimeError("macro_http_module_definitions_do_not_match_domain_modules")
for item in MACRO_HTTP_MODULES:
    version_annotation = item.persisted_schema.model_fields["schema_version"].annotation
    if get_args(version_annotation) != (MACRO_MODULE_DEFINITIONS[item.module_id].schema_version,):
        raise RuntimeError("macro_http_module_schema_version_drift")

MACRO_HTTP_MODULE_BY_ID = {item.module_id: item for item in MACRO_HTTP_MODULES}


__all__ = [
    "MACRO_HTTP_MODULES",
    "MACRO_HTTP_MODULE_BY_ID",
    "MacroHttpModuleDefinition",
]
