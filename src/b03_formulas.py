"""Version-bound B03 calculations shared by controllers, rules, meters and verifiers.

Only the standard library is required. The edition is bound once on import.
The locked simulator remains an independent reference for its own profile.
"""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sys

EDITION_PATH = Path(__file__).resolve().parents[1] / "knowledge" / "formulas-v1.json"
EDITION_SHA256 = "70870015f5ba084b6a159c5fb46e3995a6562452c97fd120157fbfdc737576fa"
EDITION_VERSION = "1.0.0"
MEMBER = "formula-edition.json"
IMPLEMENTATION = "src/b03_formulas.py"
DECLARATION = "knowledge/formulas-v1.json"


def validate_edition(raw):
    if hashlib.sha256(raw).hexdigest() != EDITION_SHA256:
        raise ValueError("Unknown formula edition digest for version " + EDITION_VERSION)
    edition = json.loads(raw)
    if edition["schema"] != "b03.formula-edition.v1" or edition["version"] != EDITION_VERSION:
        raise ValueError("Unsupported formula edition")
    return edition


EDITION_BYTES = EDITION_PATH.read_bytes()
_EDITION = validate_edition(EDITION_BYTES)
_ENTRIES = {row["id"]: row for row in _EDITION["entries"]}


def lookup(*, identifier=None, topic=None, version=EDITION_VERSION):
    if version != EDITION_VERSION or (identifier is None) == (topic is None):
        raise ValueError("Specify the supported edition and exactly one ID or topic")
    text = identifier if identifier is not None else topic
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Expected a nonempty formula ID or topic")
    if identifier is not None:
        rows = [_ENTRIES[identifier]] if identifier in _ENTRIES else []
    else:
        rows = [row for row in _ENTRIES.values() if any(topic.casefold() in word.casefold()
                for word in [row["id"], *row["topics"]])]
    return [dict(deepcopy(row), version=EDITION_VERSION, editionSha256=EDITION_SHA256) for row in rows]


def _params(identifier):
    return _ENTRIES[identifier]["parameters"]


MS_PER_S = _params("time.units")["millisecondsPerSecond"]
NS_PER_MS = _params("time.units")["nanosecondsPerMillisecond"]
NS_PER_S = NS_PER_MS * MS_PER_S
PAYLOAD_BYTES = _params("packet.wire-rate")["payloadBytes"]
EPOCH_BYTES = _params("packet.wire-rate")["epochBytes"]
TRANSPORT_BYTES = _params("packet.wire-rate")["transportBytes"]
WIRE_BYTES = PAYLOAD_BYTES + EPOCH_BYTES + TRANSPORT_BYTES
BER_CEILING = _params("path.qos.rules")["berCeiling"]
DEFAULT_HEADROOM = _params("link.capacity")["defaultControlHeadroom"]
EPS = _params("link.capacity")["solverEpsilon"]
OPENING_EPS = _params("link.capacity")["openingEpsilon"]
_LEGACY_ALLOWANCE = _params("link.capacity")["legacyVerifierAllowance"]
DEFAULT_TTL_NS = _params("observation.endpoints")["defaultObservationTtlNs"]
DEFAULT_VALIDITY_NS = _params("observation.endpoints")["defaultCommandValidityNs"]
_WIDEST_JITTER_ROOT = _params("path.qos.rules")["widestRuleJitterExponent"]
_PRUNING_ULPS = _params("path.search-bounds")["roundingUlpsPerNode"]
_RANK = _params("competition.latency")["percentileNumerator"]
_RANK_DEN = _params("competition.latency")["percentileDenominator"]


def ns_to_ms(ns):
    return ns / NS_PER_MS


def ns_to_seconds(ns):
    return ns / NS_PER_S


def seconds_to_ms(seconds):
    return seconds * MS_PER_S


def seconds_to_ns(seconds):
    return round(seconds * NS_PER_S)


def wire_bps(payload_bps, payload_bytes=PAYLOAD_BYTES, epoch_overhead_bytes=EPOCH_BYTES):
    if (type(payload_bps) is not int or payload_bps < 0 or type(payload_bytes) is not int
            or payload_bytes < 1 or type(epoch_overhead_bytes) is not int or epoch_overhead_bytes < 0):
        raise ValueError("Expected integer packet sizes and a nonnegative payload rate")
    size = payload_bytes + epoch_overhead_bytes + TRANSPORT_BYTES
    return (payload_bps * size + payload_bytes - 1) // payload_bytes


def path_metrics(parts):
    """B03 proposal metrics, using the rule edition’s capped BER."""
    parts = list(parts)
    ber = sum(p["ber"] for p in parts)
    return {"delayMs":sum(p["delayMs"] for p in parts),
            "jitterMs":math.sqrt(sum(p["jitterMs"]**2 for p in parts)),
            "ber":min(BER_CEILING, ber),
            "packetLossRate":1.0 - math.prod(1.0-p["packetLossRate"] for p in parts),
            "bottleneckBps":min((p["bandwidthBps"] for p in parts), default=None)}


def packet_quality(service, path_links, links, *, qos_profile):
    """Use the locked engine operation, including native units and the raw BER sum."""
    from resnet.control.policy import _quality
    return _quality(service, path_links, links, WIRE_BYTES, qos_profile=qos_profile)


def extend_path(delay, jitter_sq, ber, delivery, link):
    """Partial route in heap searches. Keep squared jitter and survival until comparison."""
    return (delay + link["delayMs"], jitter_sq + link["jitterMs"]**2,
            min(BER_CEILING, ber + link["ber"]), delivery * (1.0-link["packetLossRate"]))


def widest_rule_jitter(squared):
    """Preserve the independently registered widest-path rule's power rounding."""
    return squared ** _WIDEST_JITTER_ROOT


def exceeds_qos(state, requirement, epsilon=0, *, jitter_root=math.sqrt):
    delay, jitter_sq, ber, survival = state
    return (delay > requirement["delayMs"] + epsilon or jitter_root(jitter_sq) > requirement["jitterMs"] + epsilon
            or ber > requirement["ber"] + epsilon or 1.0-survival > requirement["packetLossRate"] + epsilon)


def search_vector(link):
    loss = link["packetLossRate"]
    return (link["delayMs"], link["jitterMs"] * link["jitterMs"], link["ber"],
            -math.log1p(-loss) if loss < 1.0 else math.inf)


def search_limits(requirement, nodes):
    def outward(value):
        return value + EPS + _PRUNING_ULPS * nodes * sys.float_info.epsilon * max(1., value)
    expanded = {key:outward(requirement[key]) for key in ("delayMs","jitterMs","ber","packetLossRate")}
    if expanded["ber"] >= BER_CEILING:
        expanded["ber"] = math.inf
    return search_vector(expanded)


def capacity_budget(capacity, headroom):
    return int(Decimal(capacity) * (Decimal(1) - Decimal(str(headroom))))


def legacy_verifier_budget(capacity, headroom):
    return math.floor(capacity * (1 - headroom) + _LEGACY_ALLOWANCE)


def within_capacity(after, limit, epsilon=0):
    return after <= limit + epsilon


def within_transition(after, capacity, before):
    return within_capacity(after, max(capacity, before), EPS)


def fuse_endpoints(left, right):
    return {"capacity_bps": min(left["capacity_bps"], right["capacity_bps"]),
            **{key:max(left[key],right[key]) for key in ("delay_ns","jitter_ns","ber","packet_loss_rate")}}


def fresh_sample(sampled, arrived, decision, ttl):
    return sampled <= arrived <= decision and decision - sampled <= ttl


def service_ns(duration, count):
    return duration * count


def service_losses(duration, unmet, active):
    return service_ns(duration, len(unmet)), service_ns(duration, sum(active[sid] for sid in unmet))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def time_fraction(duration, fraction):
    return (0, 0) if fraction is None else (duration * fraction, duration)


def online_pair_count(online_count):
    return online_count * (online_count - 1)


def elapsed_ns(start, end):
    """A missing delivered endpoint leaves the timing sample unobserved."""
    return None if start is None or end is None else end - start


def latency_distribution(values):
    samples = sorted(value for value in values if value is not None)
    if not samples:
        return {"n":0, "meanNs":None, "p95Ns":None, "maxNs":None}
    if samples[0] < 0:
        raise ValueError("Competition clock has a negative interval")
    size = len(samples)
    rank = (size * _RANK + _RANK_DEN - 1) // _RANK_DEN
    return {"n":size, "meanNs":sum(samples)/size, "p95Ns":samples[rank-1], "maxNs":samples[-1]}


def priority_objective(requests, target, current):
    levels = sorted({row["priority"] for row in requests.values()}, reverse=True)
    served = Counter(row["priority"] for sid,row in requests.items() if target.get(sid) is not None)
    changes = sum(current.get(sid) != target.get(sid) for sid in current.keys() | target.keys())
    return tuple(served[level] for level in levels) + (-changes,)


def priority_coefficients(priorities):
    counts = Counter(priorities)
    unit = sum(counts.values()) + 1
    weights = {}
    for priority, count in sorted(counts.items()):
        weights[priority] = unit
        unit *= count + 1
    return weights


def receipt_binding(simulation, implementation):
    control = simulation["control"]
    payload = simulation["payload_bytes"]
    if implementation.get(DECLARATION) != EDITION_SHA256 or type(payload) is not int or payload <= 0:
        raise ValueError("Formula declaration or packet profile differs from the bound edition")
    return {"schema":"b03.formula-binding.v1", "version":EDITION_VERSION, "editionSha256":EDITION_SHA256,
            "implementationSha256":implementation[IMPLEMENTATION], "effective":{
                "payloadBytes":payload, "epochBytes":EPOCH_BYTES, "transportBytes":TRANSPORT_BYTES,
                "controlHeadroom":control["control_headroom_fraction"], "qosProfile":control["qos_profile"],
                "observationTtlNs":control["observation_ttl_ns"], "commandValidityNs":control["command_validity_ns"]}}


def verify_binding(binding, archived, simulation, implementation):
    validate_edition(archived)
    expected = receipt_binding(simulation, implementation)
    if json.dumps(binding, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
        raise ValueError("Archived formula binding differs from its edition, implementation or effective parameters")
