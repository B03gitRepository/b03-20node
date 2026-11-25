"""A small replaceable topic/alias retriever over versioned public entry IDs."""
class RankedKnowledge:
    def __init__(self, config, library):
        self.rows = {r["id"]: r for r in library["entries"]}
        self.first = config.get("options",{}).get("rankFirst", "EX.PATH.DELAY")

    def lookup(self, *, identifier=None, topic=None):
        if identifier is not None:
            return [identifier] if identifier in self.rows else []
        words = set(topic.casefold().split())
        matches = {}
        for key,row in self.rows.items():
            terms = set(" ".join(row["topics"]).casefold().split())
            if key in {"EX.PATH.HOPS","EX.PATH.DELAY"}:
                terms.update(("route","guidance","detour"))
            common = len(terms & words)
            if common:
                matches[key] = common
        return sorted(matches,key=lambda k:(-matches[k], k!=self.first,k))


def build_knowledge(*, config, library):
    return RankedKnowledge(config, library)
