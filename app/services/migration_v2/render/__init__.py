"""Phase 11: anchor-based Word renderer."""

from .content import GAP_MARKER_TEXT
from .renderer import RenderBlocked, RenderError, default_ref_text, gap_slots, render_document

__all__ = ["GAP_MARKER_TEXT", "RenderBlocked", "RenderError", "default_ref_text", "gap_slots", "render_document"]
