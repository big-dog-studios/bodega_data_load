"""Prompt builders for the bodega vision classifier."""

# Stage 0 — is this even a usable image?
IMAGE_GATE = """You are validating an uploaded image for a store-products database.
Classify it as exactly one of:
  - "receipt": a store/purchase receipt or itemized bill
  - "shelf":   a photo of store shelves, coolers, racks, or products for sale
  - "other":   anything else (selfie, meme, screenshot, landscape, document, blurry/unusable, etc.)

Respond with ONLY a JSON object, no prose, no markdown:
{"kind": "receipt|shelf|other", "confidence": 0.0-1.0, "reason": "short"}"""


def extract_items(kind: str, allowed_codes: list[str]) -> str:
    """Stage 1 prompt. Hands the model the closed subtype list so it can't free-form."""
    codes = ", ".join(allowed_codes)
    common = f"""You classify store products into a fixed taxonomy.

subtype_code MUST be exactly one of these (never invent a code):
{codes}

Rules:
- display_name: the product's RETAIL NAME ONLY — brand + product (+ flavor/size if legible),
  abbreviations expanded (GV SHRD CHDR -> "Great Value Shredded Cheddar"). It is a clean
  product name, NOT a description. NEVER add location, visibility, or count notes
  ("(far right)", "(partially visible)", "(second bottle)", "(x3)") — those are not the name.
- subtype_code: the single best fit from the list. If you can read the item but not its fine type,
  use the matching *_unspecified code. Never guess a code that isn't listed.
- method: "text" if read from printed text, "visual_id" if recognized by logo/packaging only,
  "both" if confirmed by text and appearance.
- confidence: 0.0-1.0. Lower it for partial reads, glare, occlusion, or uncertain guesses.
Respond with ONLY a JSON array, no prose, no markdown."""

    if kind == "receipt":
        return common + """
For a RECEIPT, return one object per purchasable line item (skip totals, tax, change):
[{"raw": "<as printed>", "display_name": "...", "subtype_code": "...",
  "method": "text", "confidence": 0.0-1.0, "price_cents": <int or null>}]"""
    else:  # shelf
        return common + """
For a SHELF photo, scan methodically across all the shelves so you don't miss products,
but apply these limits:
- One object per DISTINCT PRODUCT, not per facing. Several identical units of the same
  product = ONE entry. Different brands/flavors = different products.
- Only include a product you can actually IDENTIFY (a real brand/product name). If all
  you can tell is a generic category (e.g. "boxed pasta", "a cereal box"), OMIT it —
  never emit a placeholder or a description as a product.
- Skip price tags, shelf labels, signage, and fixtures.
[{"raw": "<label text or ''>", "display_name": "...", "subtype_code": "...",
  "method": "...", "confidence": 0.0-1.0, "price_cents": null}]"""


def dedup_match(new_name: str, candidates: list[dict]) -> str:
    """Stage 3 part B. Only called when there's no exact match and >0 same-subtype candidates."""
    listing = "\n".join(f'  {{"id": {c["product_id"]}, "name": "{c["name"]}"}}' for c in candidates)
    return f"""A new product was detected at a store: "{new_name}".
Here are products ALREADY recorded at this store in the same category:
[
{listing}
]
Is the new product the SAME item as one of these (same product, possibly spelled/abbreviated
differently), or is it genuinely NEW? Match only if you're confident they're the same product.

Respond with ONLY JSON, no prose:
{{"match_id": <id of the existing product it matches, or null if new>, "confidence": 0.0-1.0}}"""
