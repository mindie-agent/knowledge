"""On-demand static source maps. Importing this module does not scan or start work."""

from .service import build_code_map, compare_maps, navigate

__all__ = ["build_code_map", "compare_maps", "navigate"]
