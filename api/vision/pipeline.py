"""Bodega vision classifier pipeline.

  stage 0  validate image is a receipt or shelf (reject anything else)
  stage 1  LLM extracts items and maps each to a subtype code
  stage 2  resolve code -> subtype_id; gate on confidence / unknown code
  stage 3  dedup vs existing products at this store: name-embedding distance,
           LLM only for the ambiguous gray band
  stage 4  matched -> update price if newly known; new -> insert (with embedding)

Stateless: every run reloads the taxonomy. Same code local or as a Cloud Run Job.
"""
import base64, json, mimetypes
import numpy as np
from dataclasses import dataclass, field
from anthropic import Anthropic

from . import prompts, db, embed

MODEL = "claude-haiku-4-5-20251001"   # cheap model: image gate + text dedup judge
EXTRACT_MODEL = "claude-sonnet-4-6"   # stronger vision model for exhaustive item extraction
CONF_MIN   = 0.55     # extraction confidence floor -> else review
GATE_MIN   = 0.60     # image-type confidence floor -> else reject
DEDUP_MIN  = 0.70     # LLM match confidence (gray band) to call it the same product
DIST_LOW   = 0.12     # cosine distance below -> confident MATCH (no LLM)   [tune]
DIST_HIGH  = 0.30     # cosine distance above -> confident NEW   (no LLM)   [tune]

client = Anthropic()  # ANTHROPIC_API_KEY from env / Secret Manager


@dataclass
class Result:
    kind: str = "other"
    applied: list = field(default_factory=list)
    review: list = field(default_factory=list)
    rejected_reason: str | None = None


# ---------- LLM plumbing ----------
def _image_block(image_bytes, media_type):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
            "data": base64.standard_b64encode(image_bytes).decode()}}


def _ask_json(system, content_blocks, max_tokens=2000, model=MODEL):
    # temperature=0: deterministic, faithful reads — minimizes the model inventing
    # plausible-but-fake brand names it can't actually read off the packaging.
    msg = client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                 temperature=0,
                                 messages=[{"role": "user", "content": content_blocks}])
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip()
    return json.loads(text)


def _cos_dist(a, b):
    a = np.asarray(a, dtype=float); b = np.asarray(b, dtype=float)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - float(np.dot(a, b) / (na * nb))


# ---------- stages ----------
def stage0_gate(image_bytes, media_type):
    out = _ask_json(prompts.IMAGE_GATE, [_image_block(image_bytes, media_type)], max_tokens=200)
    return out.get("kind", "other"), float(out.get("confidence", 0))


def stage0_verify(image_bytes, media_type, claimed_kind):
    """Targeted gate: the uploader claims `claimed_kind` — confirm ONLY that (yes/no),
    never reclassify into the other type. Returns (matches_claim, confidence)."""
    out = _ask_json(prompts.verify_kind(claimed_kind),
                    [_image_block(image_bytes, media_type)], max_tokens=200)
    return bool(out.get("match", False)), float(out.get("confidence", 0))


def stage1_extract(image_bytes, media_type, kind, dsn):
    system = prompts.extract_items(kind, db.allowed_codes(dsn))
    items = _ask_json(system,
                      [_image_block(image_bytes, media_type),
                       {"type": "text", "text": f"This is a {kind}. List EVERY distinct "
                        "product you can see — go shelf by shelf, left to right; do not skip or summarize."}],
                      max_tokens=8000, model=EXTRACT_MODEL)
    return items if isinstance(items, list) else []


def stage3_dedup(conn, license_number, subtype_id, display_name):
    """Returns (status, product_id):
        ("match", id)        same product exists at this store
        ("new", None)        genuinely new
        ("ambiguous", id)    unsure -> caller routes to review, does NOT write
    Semantic, source-agnostic. Vector distance over the tiny store+subtype set;
    LLM only for the gray band. Embedding is NAME-ONLY (symmetric with stored)."""
    cands = db.candidates_at_store(conn, license_number, subtype_id)
    if not cands:
        return ("new", None)

    norm = lambda s: " ".join(s.lower().split())
    for c in cands:                                   # free exact fast-path
        if norm(c["name"]) == norm(display_name):
            return ("match", c["product_id"])

    qv = embed.embed_one(display_name)                # 1 cheap embed call
    scored = [(c, _cos_dist(qv, c["embedding"])) for c in cands if c["embedding"] is not None]
    if not scored:                                    # candidates not yet embedded
        return ("ambiguous", cands[0]["product_id"])  # be safe -> review, don't dup
    scored.sort(key=lambda x: x[1])
    nearest, dist = scored[0]

    if dist < DIST_LOW:
        return ("match", nearest["product_id"])
    if dist > DIST_HIGH:
        return ("new", None)

    # gray band -> let the LLM judge against the few nearest
    near = [c for c, _ in scored[:3]]
    # The prompt rides as `system`; the user content must be non-empty (Anthropic
    # rejects an empty user message), so send a minimal text block to trigger the answer.
    out = _ask_json(prompts.dedup_match(display_name, near),
                    [{"type": "text", "text": "Return the JSON verdict now."}], max_tokens=200)
    mid, conf = out.get("match_id"), float(out.get("confidence", 0))
    if mid is None:
        return ("new", None)
    if conf >= DEDUP_MIN:
        return ("match", int(mid))
    return ("ambiguous", int(mid))


# ---------- orchestration ----------
def process(image_bytes, license_number, dsn, media_type="image/jpeg", kind=None, photo_ref=None):
    """Classify one image and act on it.

    `kind` is the uploader's own classification, when known:
      - 'receipt' | 'shelf'  -> a specific claim. We don't trust it: verify ONLY that
        claim (stage0_verify) and reject a mismatch or garbage; never re-bucket.
      - 'general'            -> "a store photo, but I'm not saying which". The open gate
        decides shelf vs storefront; a receipt or unusable image is rejected.
      - None                 -> fully open gate (receipt | shelf | storefront | other).

    A storefront is not a catalog image — there are no products to extract. We attach it
    to the store instead (stores.storefront_photos) and return. `photo_ref` is the durable
    GCS path to record; without it (a raw upload we didn't persist) there's nothing to
    store, so the storefront is recognized but not attached."""
    res = Result()

    if kind in ("receipt", "shelf"):
        ok, gconf = stage0_verify(image_bytes, media_type, kind)
        res.kind = kind
        if not ok or gconf < GATE_MIN:
            res.rejected_reason = f"does not look like a {kind} (conf={gconf:.2f})"
            return res
    elif kind == "general":
        kind, gconf = stage0_gate(image_bytes, media_type)
        res.kind = kind
        if kind not in ("shelf", "storefront") or gconf < GATE_MIN:
            res.rejected_reason = f"not a shelf/storefront (kind={kind}, conf={gconf:.2f})"
            return res
    else:
        kind, gconf = stage0_gate(image_bytes, media_type)
        res.kind = kind
        if kind == "other" or gconf < GATE_MIN:
            res.rejected_reason = f"unusable image (kind={kind}, conf={gconf:.2f})"
            return res

    # Storefront: no catalog to extract — attach the exterior photo to the store.
    if kind == "storefront":
        if photo_ref:
            with db.connect(dsn) as conn:
                db.add_storefront_photo(conn, license_number, photo_ref)
                conn.commit()
            res.applied.append({"action": "storefront_photo", "url": photo_ref})
        else:
            res.rejected_reason = "storefront recognized but no durable url to attach"
        return res

    items = stage1_extract(image_bytes, media_type, kind, dsn)
    sid_map = db.subtype_id_map(dsn)
    source = "receipt" if kind == "receipt" else "shelf_photo"

    handled = set()  # product_ids already acted on this scan -> collapse model dupes
    with db.connect(dsn) as conn:
        for it in items:
            name = (it.get("display_name") or "").strip()
            code = it.get("subtype_code")
            conf = float(it.get("confidence", 0))
            price = it.get("price_cents")
            raw = it.get("raw")

            if not name or code not in sid_map:
                res.review.append({"name": name or raw, "reason": f"unknown subtype_code: {code}"})
                continue
            if conf < CONF_MIN:
                res.review.append({"name": name, "reason": f"low confidence {conf:.2f}", "code": code})
                continue
            subtype_id = sid_map[code]

            status, pid = stage3_dedup(conn, license_number, subtype_id, name)

            if status == "ambiguous":
                res.review.append({"name": name, "reason": "ambiguous dedup match",
                                   "maybe_product_id": pid, "code": code})
                continue

            if status == "new":
                emb = embed.embed_one(name)
                pid = db.insert_product(conn, license_number, subtype_id, name, price, raw,
                                        source, source_category=source, embedding=emb)

            # Collapse within-scan duplicates: the model often lists the same product
            # twice (packaging/size variants), which dedup resolves to one product_id.
            # Act on each product_id once per scan so the response has no repeats.
            if pid in handled:
                continue
            handled.add(pid)

            if status == "new":
                res.applied.append({"action": "insert", "product_id": pid,
                                    "name": name, "subtype": code})
            elif price is not None:
                db.update_price(conn, pid, price, raw, source)
                res.applied.append({"action": "price_update", "product_id": pid,
                                    "name": name, "subtype": code})
            else:
                res.applied.append({"action": "exists_no_change", "product_id": pid,
                                    "name": name, "subtype": code})
        conn.commit()
    return res


def process_file(path, license_number, dsn):
    media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        return process(f.read(), license_number, dsn, media_type)
