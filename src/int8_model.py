"""CPU inference with dynamically quantized Linear operators on the pinned frozen model."""
import platform
import time

from frozen_policy import FrozenActor


def quantize_linears(model, torch):
    """Require every eligible Linear to be packed with qint8 weights and report its coverage."""
    dynamic_linear = torch.ao.nn.quantized.dynamic.Linear
    linear = torch.nn.Linear
    shapes = {name: tuple(module.weight.shape) for name, module in model.named_modules()
              if type(module) is linear}
    if not shapes:
        raise ValueError("The model contains no standard Linear layers to quantize")
    original_parameters = sum(p.numel() for p in model.parameters())
    torch.ao.quantization.quantize_dynamic(model, {linear}, dtype=torch.qint8, inplace=True)
    count = 0
    for name, shape in shapes.items():
        module = model.get_submodule(name)
        if not isinstance(module, dynamic_linear):
            raise RuntimeError(f"Linear operator did not convert to the qint8 backend: {name}")
        weight = module.weight()
        if weight.dtype != torch.qint8 or tuple(weight.shape) != shape:
            raise RuntimeError(f"Unexpected packed weight format: {name}")
        count += weight.numel()
        del weight
    return {"linearModules": len(shapes), "quantizedLinearModules": len(shapes),
            "linearWeightElements": count, "quantizedLinearWeightElements": count,
            "originalUniqueParameterElements": original_parameters,
            "remainingFloatParameterElements": sum(p.numel() for p in model.parameters() if p.is_floating_point()),
            "quantizedDtype": "qint8", "kernel": "quantized::linear_dynamic"}


def build_model(*, checkpoint, tokenizer, device, config):
    import torch
    if torch.device(device).type != "cpu":
        raise ValueError("The x86 dynamic INT8 model needs --device cpu")
    if platform.machine().lower() not in ("x86_64", "amd64"):
        raise ValueError("The x86 dynamic INT8 model requires a 64-bit x86 CPU")
    backend = config.get("backend", "x86")
    if backend not in ("x86", "fbgemm") or backend not in torch.backends.quantized.supported_engines:
        raise ValueError("The installed PyTorch build lacks the selected x86 quantized backend")
    torch.backends.quantized.engine = backend
    actor = FrozenActor(checkpoint, tokenizer, "cpu")
    start = time.perf_counter_ns()
    coverage = quantize_linears(actor.model, torch)
    actor.measure_forward = True
    modules_seen = set()

    def mark_used(module, inputs, output):
        modules_seen.add(id(module))

    for module in actor.model.modules():
        if isinstance(module, torch.ao.nn.quantized.dynamic.Linear):
            module.register_forward_hook(mark_used)
    original_choose = actor.choose

    def choose(observation):
        modules_seen.clear()
        action, details = original_choose(observation)
        if len(modules_seen) != coverage["quantizedLinearModules"]:
            raise RuntimeError("The prefill did not execute every converted Linear operator")
        return action, dict(details, executedInt8LinearModules=len(modules_seen))

    actor.choose = choose
    actor.identity = dict(actor.identity, precision="dynamic-int8-linear",
                          quantization={**coverage, "engine": backend, "threads": torch.get_num_threads(),
                                        "precisionOther": "float32", "conversionWallNs": time.perf_counter_ns() - start})
    return actor
