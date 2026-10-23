"""Edge Engine: prices player props from sharp books, exchanges and your own projections,
logs every pick to a tamper-evident record, and builds sized pick'em entries."""
__version__ = "3.0.0"
MODEL_VERSION = "blend-2"   # blend-2: no preseason data, projection limited to +-5 pts, no questionable legs
# Versions replaced because of a pricing bug. Their picks stay in the hash chain (nothing is ever
# deleted), but the track record's headline numbers count only picks from models still in use.
RETIRED_MODELS = {
    "blend-1": "Retired Oct 23, 2026: preseason box scores and an uncapped projection inflated leg odds.",
}
