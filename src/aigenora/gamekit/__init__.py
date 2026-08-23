"""Deterministic, local-first game bundle compiler for Aigenora."""

from .compiler import (
    GAME_KIT_SCHEMA,
    SUPPORTED_PRESETS,
    GameKitError,
    compile_game,
    default_manifest,
    has_game_blueprint,
    inspect_game_spec,
    materialize_protocol,
    normalize_manifest,
    validate_game_bundle,
)

__all__ = [
    "GAME_KIT_SCHEMA",
    "SUPPORTED_PRESETS",
    "GameKitError",
    "compile_game",
    "default_manifest",
    "has_game_blueprint",
    "inspect_game_spec",
    "materialize_protocol",
    "normalize_manifest",
    "validate_game_bundle",
]
