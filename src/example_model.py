"""A separately registered adapter for a controlled comparison using the pinned weights."""

from frozen_policy import FrozenActor


class ReferenceFrozenActor(FrozenActor):
    def choose(self, observation):
        selected, details = super().choose(observation)
        return selected, dict(details, policyVersion=self.version)


def build_model(*, checkpoint, tokenizer, device, config):
    if config["mode"] != "frozen" or not checkpoint:
        raise ValueError("The reference frozen adapter requires frozen mode and a pinned checkpoint")
    return ReferenceFrozenActor(checkpoint, tokenizer, device)
