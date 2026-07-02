"""Modular, individually testable value/path parsers for the Token Normalizer.

Each parser is a pure function: deterministic, no I/O, no side effects. They are
intentionally small and composable (KISS/YAGNI) so new patterns from real client
Figma files can be added one function at a time rather than touching a monolith.
"""
from parsers.color import parse_color, parse_hex, parse_rgba
from parsers.dimension import parse_dimension
from parsers.paths import css_var_to_dotted, enrichment_path_to_dotted
from parsers.shadow import parse_box_shadow, split_top_level
from parsers.typography import parse_font_family, parse_typography

__all__ = [
    "parse_color",
    "parse_hex",
    "parse_rgba",
    "parse_dimension",
    "parse_typography",
    "parse_font_family",
    "parse_box_shadow",
    "split_top_level",
    "css_var_to_dotted",
    "enrichment_path_to_dotted",
]
