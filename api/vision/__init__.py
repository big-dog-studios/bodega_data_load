"""Vision catalog pipeline: image (receipt/shelf) -> products at a store.

Imported lazily by the API (`from vision import pipeline`) so the read path
doesn't gain a hard dependency on ANTHROPIC_API_KEY / Vertex at startup.
See vision/pipeline.py for the stage-by-stage flow.
"""
