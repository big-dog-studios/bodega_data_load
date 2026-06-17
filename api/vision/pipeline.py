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

MODEL = "claude-haiku-4-5-20251001"   # verify current string in docs before deploy
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


def _ask_json(system, content_blocks, max_tokens=2000):
    msg = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
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


def stage1_extract(image_bytes, media_type, kind, dsn):
    system = prompts.extract_items(kind, db.allowed_codes(dsn))
    items = _ask_json(system, [_image_block(image_bytes, media_type),
                               {"type": "text", "text": f"This is a {kind}. Extract items."}])
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
    out = _ask_json(prompts.dedup_match(display_name, near), [], max_tokens=200)
    mid, conf = out.get("match_id"), float(out.get("confidence", 0))
    if mid is None:
        return ("new", None)
    if conf >= DEDUP_MIN:
        return ("match", int(mid))
    return ("ambiguous", int(mid))


# ---------- orchestration ----------
def process(image_bytes, license_number, dsn, media_type="image/jpeg"):
    res = Result()

    kind, gconf = stage0_gate(image_bytes, media_type)
    res.kind = kind
    if kind == "other" or gconf < GATE_MIN:
        res.rejected_reason = f"not a receipt/shelf (kind={kind}, conf={gconf:.2f})"
        return res

    items = stage1_extract(image_bytes, media_type, kind, dsn)
    sid_map = db.subtype_id_map(dsn)
    source = "receipt" if kind == "receipt" else "shelf_photo"

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

            if status == "match":
                if price is not None:
                    db.update_price(conn, pid, price, raw, source)
                    res.applied.append({"action": "price_update", "product_id": pid,
                                        "name": name, "subtype": code})
                else:
                    res.applied.append({"action": "exists_no_change", "product_id": pid,
                                        "name": name, "subtype": code})
            elif status == "ambiguous":
                res.review.append({"name": name, "reason": "ambiguous dedup match",
                                   "maybe_product_id": pid, "code": code})
            else:  # new
                emb = embed.embed_one(name)
                pid = db.insert_product(conn, license_number, subtype_id, name, price, raw,
                                        source, source_category=source, embedding=emb)
                res.applied.append({"action": "insert", "product_id": pid,
                                    "name": name, "subtype": code})
        conn.commit()
    return res


def process_file(path, license_number, dsn):
    media_type = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        return process(f.read(), license_number, dsn, media_type)
