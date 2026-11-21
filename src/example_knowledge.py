"""An in-memory ID and topic index over one released knowledge edition."""


class TopicIndex:
    def __init__(self, library):
        self.ids = {row["id"] for row in library["entries"]}
        self.by_topic = {}
        for row in library["entries"]:
            for topic in row["topics"]:
                self.by_topic.setdefault(topic.casefold(), set()).add(row["id"])

    def lookup(self, *, identifier=None, topic=None):
        if identifier is not None:
            return [identifier] if identifier in self.ids else []
        return sorted({sid for label, ids in self.by_topic.items() if topic.casefold() in label for sid in ids})


def build_knowledge(*, config, library):
    if library["version"] != config["version"]:
        raise ValueError("The index and registration have different editions")
    return TopicIndex(library)
