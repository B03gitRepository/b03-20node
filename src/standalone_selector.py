"""A dependency-free frozen tool selector. Replace this file with your own adapter."""


class Selector:
    version = "example-1"

    def choose(self, observation):
        legal = observation["legalActions"]
        catalog = observation["actionCatalog"]
        scopes = observation["authorizedServiceSets"]
        for tool in ("priority_greedy", "bounded_search"):
            for action in legal:
                entry = catalog[action]
                if entry["tool"] == tool and scopes[entry["serviceSet"]]:
                    return action, {"policyVersion": self.version}
        return legal[0], {"policyVersion": self.version}


def build_model(*, checkpoint, tokenizer, device, config):
    return Selector()
