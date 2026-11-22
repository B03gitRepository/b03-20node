"""在独立进程中记录指定窗口的运行耗时和本进程峰值内存。

该数字只代表本机、本配置及当时负载；不据此推断比 ns-3 更快。
"""
from pathlib import Path
import argparse
import json
import platform
import sys
import time

from resnet.simulation.simulation import Simulation


def peak_memory_bytes():
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counter = Counters()
        counter.cb = ctypes.sizeof(counter)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counter), counter.cb):
            raise OSError(ctypes.get_last_error())
        return counter.PeakWorkingSetSize
    import resource
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == 'darwin' else raw * 1024


def main(argv=None):
    # 性能测量复用实验配置，避免另写控制节点、主备链路等场景参数。
    from resnet.application.config import load_config
    from resnet.application.cli import PROJECT_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=PROJECT_ROOT/'configs/experiment.json')
    parser.add_argument('--case', default='quick')
    parser.add_argument('--seconds', type=float, default=None, help='可选：从案例起点运行的秒数')
    parser.add_argument('--mode', choices=['fixed','control'], default=None)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError('请使用新的性能结果文件名')
    scenario, config, source, config_hash = load_config(args.config, args.case)
    if args.mode is not None:
        config['mode'] = args.mode
    if args.seconds is not None:
        if args.seconds <= 0:
            raise ValueError('seconds 必须大于零')
        config['end_s'] = config.get('start_s', 0) + args.seconds
    config.update(drain_s=0, sample_modulus=0, log_control=False)
    started = time.perf_counter()
    sim = Simulation(scenario, config)
    try:
        sim.advance(sim.end_ns)
        elapsed = time.perf_counter()-started
        summary = sim.summary()
        result = dict(scope='single_process_no_file_trace',
            scenario_source=str(source), config_sha256=config_hash, effective_config=sim.config,
            simulation_seconds=(sim.end_ns-sim.start_ns)/1e9,
            mode=sim.config['mode'], wall_elapsed_s=elapsed, peak_process_memory_bytes=peak_memory_bytes(),
            python=platform.python_version(), platform=platform.platform(),
            packets_generated=summary['totals']['data']['generated_packets'],
            conservation=summary['conservation'], checks_passed=summary['checks_passed'],
            limitation='Measured on this machine; concurrent load affects wall time; includes state initialization')
    finally:
        sim.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['checks_passed'] else 1
