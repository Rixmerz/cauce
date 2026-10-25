def tags_of(line) -> set[str]:
    """A line's tags: the catalog sends them as one comma-separated string."""
    return {t.strip().lower() for t in (line.get("tags") or "").split(",") if t.strip()}


def amount(line) -> int:
    return line["price"] * line["qty"]
