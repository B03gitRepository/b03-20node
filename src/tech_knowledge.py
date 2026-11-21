"""Look up released technical notes by stable ID or topic without opening a simulator."""
import json


def query(components, *, identifier=None, topic=None):
    if (identifier is None) == (topic is None):
        raise ValueError("Select exactly one knowledge ID or topic")
    library = json.loads(components.knowledge_path.read_bytes())
    edition = components.knowledge["version"]
    if library.get("schema") != "b03.knowledge.v1" or library.get("version") != edition:
        raise ValueError("The configured technical knowledge edition differs from the source")
    seen = set()
    selected = []
    for entry in library["entries"]:
        required = ("id", "topics", "content", "sources", "applicability")
        if any(not entry.get(key) for key in required) or entry["id"] in seen:
            raise ValueError("Incomplete or duplicate knowledge entry")
        seen.add(entry["id"])
        if (identifier is not None and entry["id"] == identifier) or (
            topic is not None and topic.strip() and any(topic.casefold() in label.casefold() for label in entry["topics"])
        ):
            selected.append({**entry, "version": edition})
    return selected
