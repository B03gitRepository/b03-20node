"""A portable physical-port detector, using the simulator's four public data callbacks."""


class LocalPortDetector:
    id = "example.public-port-probes"
    version = "1.0.0"
    contractVersion = "b03.packet-simulator.v2"

    def __init__(self, delay):
        if type(delay) is not int or delay <= 0:
            raise ValueError("detectionDelayNs must be a positive integer")
        self.delay = delay

    def on_event(self, event):
        if self.delay >= event["remainingNs"]:
            return []
        return [{"op": "probe", "nodeId": node, "delayNs": self.delay} for node in event["probeNodes"]]

    def on_observation(self, record):
        if record["sampledAtNs"] > record["arrivedAtNs"]:
            raise ValueError("Observation predates its sample")

    def on_device(self, record):
        at = record["executedAtNs"]
        if at is not None and at > record["timeNs"]:
            raise ValueError("Future device execution")

    def on_meter(self, interval):
        span = interval["endNs"] - interval["startNs"]
        if span <= 0 or interval["unmetServiceNs"] != span * interval["unmetCount"]:
            raise ValueError("Inconsistent business interval")


def build(settings):
    if set(settings) != {"detectionDelayNs"}:
        raise ValueError("Set detectionDelayNs for the local port detector")
    return LocalPortDetector(settings["detectionDelayNs"])
