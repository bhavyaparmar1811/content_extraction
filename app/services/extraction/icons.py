"""Icon extractor using perceptual hashing and heuristics.

Detects if extracted images are actually semantic icons (e.g., Warning, PPE)
and converts them from generic images to ExtractedIcon elements.
"""

import json
import shutil
from pathlib import Path

import imagehash
from PIL import Image

from app.schemas.document import (
    ElementType,
    ExtractedElement,
    ExtractedIcon,
    RawDocument,
)
from app.schemas.ast_nodes import IconCategory
from app.config.settings import Settings
from .base_extractor import BaseExtractor


class IconExtractor(BaseExtractor):
    """Converts images into icons if they match known heuristics or hashes."""

    # Hamming distance threshold for matching perceptual hashes.
    HASH_THRESHOLD = 5

    def __init__(self, settings: Settings, logger=None):
        super().__init__(settings, logger)
        self._reference_library = self._load_reference_library()

    def _load_reference_library(self) -> dict[str, list[imagehash.ImageHash]]:
        """Load icon reference hashes from data/config/icon_library.json"""
        library_path = Path("data/config/icon_library.json")
        if not library_path.exists():
            # Create a default empty template
            library_path.parent.mkdir(parents=True, exist_ok=True)
            default_data = {
                "warning": [],
                "ppe": [],
                "info": []
            }
            library_path.write_text(json.dumps(default_data, indent=2))
            return {}

        try:
            data = json.loads(library_path.read_text())
            return {
                meaning: [imagehash.hex_to_hash(h) for h in hashes]
                for meaning, hashes in data.items()
            }
        except Exception as e:
            self.logger.warning(f"Failed to load icon library: {e}")
            return {}

    def _convert_to_icon_if_applicable(
        self, element: ExtractedElement, document_id: str | None = None, is_template: bool = False
    ) -> ExtractedElement:
        """Check if an image element is an icon candidate and convert it."""
        if element.element_type != ElementType.IMAGE:
            return element

        # A picture displayed large is a figure, whatever its file resolution.
        # DOCX images carry their displayed size (px at 96 dpi); 0 means unknown.
        shown_w, shown_h = getattr(element, "width", 0) or 0, getattr(element, "height", 0) or 0
        if max(shown_w, shown_h) > 128:
            return element

        img_path = Path(element.image_path)
        if not img_path.exists() and document_id:
            icon_dir = self.settings.get_template_icon_dir(document_id) if is_template else self.settings.get_document_icon_dir(document_id)
            candidate_icon_path = icon_dir / img_path.name
            if candidate_icon_path.exists():
                img_path = candidate_icon_path
        if not img_path.exists():
            return element

        try:
            with Image.open(img_path) as img:
                width, height = img.size
                is_icon_candidate = width <= 128 and height <= 128
                if not is_icon_candidate:
                    return element
                current_hash = imagehash.average_hash(img)
        except Exception as e:
            self.logger.warning(f"IconExtractor: Failed to process {img_path}: {e}")
            return element

        matched_meaning = None
        for meaning, hashes in self._reference_library.items():
            for known_hash in hashes:
                if current_hash - known_hash <= self.HASH_THRESHOLD:
                    matched_meaning = meaning
                    break
            if matched_meaning:
                break

        if matched_meaning or is_icon_candidate:
            final_path = img_path
            if document_id:
                if is_template:
                    icon_dir = self.settings.get_template_icon_dir(document_id)
                else:
                    icon_dir = self.settings.get_document_icon_dir(document_id)
                new_path = icon_dir / img_path.name
                try:
                    if img_path.exists() and img_path != new_path:
                        shutil.move(str(img_path), str(new_path))
                        final_path = new_path
                    elif new_path.exists():
                        final_path = new_path
                except Exception as e:
                    self.logger.warning(f"IconExtractor: Failed to move icon to {new_path}: {e}")

            icon_el = ExtractedIcon(
                content=f"[Icon: {matched_meaning or 'unknown'}]",
                page=element.page,
                sequence=element.sequence,
                bbox=element.bbox,
                icon_id=str(current_hash),
                semantic_meaning=matched_meaning or "unknown",
                image_path=str(final_path),
                content_hash=getattr(element, "content_hash", None),
                metadata={
                    **(getattr(element, "metadata", None) or {}),
                    **({"width_px": shown_w, "height_px": shown_h} if shown_w and shown_h else {}),
                },
            )
            self.logger.debug(f"Identified icon: {matched_meaning or 'unknown'}")
            return icon_el

        return element

    def extract(self, document: RawDocument, document_id: str | None = None, is_template: bool = False) -> RawDocument:
        self.logger.info(f"IconExtractor: processing {document.source}")

        for page in document.pages:
            new_elements = []
            
            for element in page.elements:
                if element.element_type == ElementType.TABLE:
                    # Process cell media in table headers and rows
                    for header_cell in getattr(element, "headers", []):
                        if getattr(header_cell, "media_nodes", None):
                            header_cell.media_nodes = [
                                self._convert_to_icon_if_applicable(m, document_id, is_template)
                                for m in header_cell.media_nodes
                            ]
                    for row in getattr(element, "rows", []):
                        for cell in row:
                            if getattr(cell, "media_nodes", None):
                                cell.media_nodes = [
                                    self._convert_to_icon_if_applicable(m, document_id, is_template)
                                    for m in cell.media_nodes
                                ]
                    new_elements.append(element)
                elif element.element_type == ElementType.IMAGE:
                    converted = self._convert_to_icon_if_applicable(element, document_id, is_template)
                    new_elements.append(converted)
                else:
                    new_elements.append(element)

            page.elements = new_elements

        return document
