"""将报告数据画成通用曲线与事件图；不保存另一份场景事件安排。

输入：运行目录，或 comparison.py 生成的 analysis.json。
输出：PNG（对照时间图同时提供 SVG）。Matplotlib 仅在实际绘图时导入。
"""
import argparse
from pathlib import Path
import textwrap

from .data import EVENT_NAMES, collect_report, read_json
from .html import MODE_NAMES, event_detail


def _plotting(font=None):
    """延迟加载绘图依赖，普通仿真和 HTML 报告无需安装 Matplotlib。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    if font is None:
        candidate = Path('C:/Windows/Fonts/msyh.ttc')
        font = candidate if candidate.is_file() else None
    elif not Path(font).is_file():
        raise FileNotFoundError(f'字体文件不存在：{font}')
    prop = FontProperties(fname=str(font)) if font else FontProperties()
    return plt, prop


def _curves(plt, font, runs):
    """共用的多指标曲线：速率、加权时延、窗口丢包与队列采样峰值。"""
    figure, axes = plt.subplots(4, 1, figsize=(12, 9), sharex=True)
    fields = [('goodput_mbps', '交付负载 Mbps'), ('delay_ms', '已收包平均时延 ms'),
              ('dropped', '窗口终结丢包数'), ('sampled_queue_max', '等待队列采样峰值 / 包')]
    colors = ('#35719c', '#188377', '#936cb0')
    all_events = set()
    names = []
    for index, report in enumerate(runs):
        name = report['scenario_id'] or '网络仿真'
        if name not in names:
            names.append(name)
        mode = MODE_NAMES.get(report['config']['mode'], report['config']['mode'])
        label = f'{mode} · {report["case_id"]}'
        points = report['series']['points']
        for ax, (key, _) in zip(axes, fields):
            if any(p[key] is not None for p in points):
                ax.plot([p['time_s'] for p in points],
                        [p[key] if p[key] is not None else float('nan') for p in points],
                        color=colors[index % len(colors)], label=label, linewidth=1.5)
        all_events.update(e['time_s'] for e in report['events'])
        for ax in axes:
            ax.axvline(report['config']['end_s'], color='#777', linestyle='--', linewidth=.7)
    for ax, (_, label) in zip(axes, fields):
        ax.set_ylabel(label, fontproperties=font)
        ax.set_ylim(bottom=0)
        ax.grid(alpha=.15)
        if len(all_events) <= 60:
            for time_s in sorted(all_events):
                ax.axvline(time_s, color='#ccc', linewidth=.5, zorder=0)
        if not ax.lines:
            ax.text(.5, .5, '本轮未记录该指标', transform=ax.transAxes,
                    ha='center', fontproperties=font)
    # 缺少遥测时画明确说明；不把缺测队列填成零。
    if all(not r['telemetry'] for r in runs):
        axes[-1].text(.5, .5, '未记录网络采样，队列曲线不可用',
                      transform=axes[-1].transAxes, ha='center', fontproperties=font)
    axes[0].legend(prop=font, frameon=False)
    axes[-1].set_xlabel('仿真时间 s', fontproperties=font)
    axes[-1].set_xlim(0, max(r['stop_s'] for r in runs))
    figure.suptitle(' / '.join(names) + ' · 网络运行对照', fontproperties=font, fontsize=16)
    figure.text(.07, .015, '竖虚线为各轮停源时刻；浅灰线为实际事件。曲线按报告时间窗聚合，缺少收包时不绘制零时延。',
                fontproperties=font, fontsize=9)
    figure.tight_layout(rect=(0, .04, 1, .96))
    return figure


def plot_runs(paths, output, font=None):
    """为一个或多个完整结果目录生成曲线；时间与标题取自各轮结果。"""
    reports = [collect_report(path) for path in paths]
    if not reports:
        raise ValueError('至少需要一个结果目录')
    plt, prop = _plotting(font)
    figure = _curves(plt, prop, reports)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(target, dpi=180, facecolor='white')
    plt.close(figure)
    return target


def plot_comparison(directory, font=None):
    """从已保存的对照数据生成三组图；不读取或修改场景和运行日志。

    图一逐行列实际事件及更新参数；图二展示全程响应；图三比较活动业务。
    事件数量、业务 ID、仿真时长均由输入决定，适用于新节点/链路/事件剧本。
    """
    root = Path(directory)
    data = read_json(root / 'analysis.json')
    if data.get('schema') != 'resnet-comparison-v1':
        raise ValueError('需要 resnet-comparison-v1 对照数据，请先执行 analyze_timeline')
    plt, prop = _plotting(font)
    outputs = []

    def save(figure, name):
        for suffix in ('.png', '.svg'):
            target = root / (name + suffix)
            figure.savefig(target, dpi=180, facecolor='white')
            outputs.append(target)
        plt.close(figure)

    events = data['events']
    fig, ax = plt.subplots(figsize=(14, max(3.5, 1.5 + len(events) * .52)))
    colors = {'link_param_change': '#35719c', 'service_adjust': '#8560a7',
              'node_move': '#20867c', 'node_offline': '#c46f32',
              'node_online': '#20867c', 'link_state': '#b95151'}
    if events:
        labels = []
        for index, event in enumerate(events):
            target, detail = event_detail(event)
            labels.append(f'{event["id"]}  {EVENT_NAMES.get(event["type"], event["type"])}  {target}')
            ax.hlines(index, 0, data['stop_s'], color='#ddd', linewidth=.7)
            ax.scatter(event['time_s'], index, color=colors.get(event['type'], '#666'), s=35)
            align = 'right' if event['time_s'] > data['stop_s'] * .6 else 'left'
            annotation = textwrap.fill(f'{event["time_s"]:g} s  {detail}', width=65)
            ax.annotate(annotation, (event['time_s'], index),
                        xytext=(-6 if align == 'right' else 6, 7), textcoords='offset points',
                        ha=align, fontproperties=prop, fontsize=8)
        ax.set_yticks(range(len(labels)), labels, fontproperties=prop, fontsize=9)
        # 首行留白容纳参数注释，防止标签与图标题重叠。
        ax.set_ylim(len(events) - .5, -.7)
    else:
        ax.text(.5, .5, '本轮没有执行核心动态事件', transform=ax.transAxes,
                ha='center', fontproperties=prop)
        ax.set_yticks([])
    ax.set_xlim(0, data['stop_s'])
    ax.set_xlabel('仿真时间 s', fontproperties=prop)
    ax.set_title(f'{data["scenario_id"]} · 实际事件时间轴', fontproperties=prop, fontsize=15)
    ax.grid(axis='x', alpha=.15)
    fig.tight_layout()
    save(fig, '01_事件时间轴')

    reports = list(data['runs'].values())
    save(_curves(plt, prop, reports), '02_网络运行对照')
    selected = sorted({s['id'] for report in reports for s in report['services'] if s['demand_bits'] > 0})
    fig, axes = plt.subplots(1, 2, figsize=(12, max(3.5, len(selected) * .35 + 1.5)), sharey=True)
    for axis, key, title in zip(axes, ('demand_delivery_pct', 'demand_timely_pct'),
                                ('交付负载 / 活动需求', '按时交付负载 / 活动需求')):
        for index, report in enumerate(reports):
            values = {s['id']: s for s in report['services']}
            axis.barh([i + (index - .5) * .35 for i in range(len(selected))],
                      [values[sid][key] or 0 for sid in selected], height=.35,
                      label=MODE_NAMES.get(report['config']['mode'], report['config']['mode']))
        axis.set_title(title, fontproperties=prop)
        axis.set_xlabel('%')
        axis.axvline(100, color='#777', linestyle=':', linewidth=.8)
        axis.grid(axis='x', alpha=.15)
    axes[0].set_yticks(range(len(selected)), selected, fontsize=9)
    axes[0].invert_yaxis()
    if not selected:
        axes[0].text(.5, .5, '本轮无活动业务', transform=axes[0].transAxes,
                     ha='center', fontproperties=prop)
    axes[1].legend(prop=prop)
    fig.suptitle(f'{data["scenario_id"]} · 逐业务完成度', fontproperties=prop, fontsize=15)
    fig.tight_layout()
    save(fig, '03_逐业务完成度')
    return outputs


def main(argv=None):
    parser = argparse.ArgumentParser(description='从实际运行结果生成通用多指标曲线')
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--font', type=Path)
    args = parser.parse_args(argv)
    print(plot_runs(args.runs, args.output, args.font))
    return 0


def timeline_main(argv=None):
    parser = argparse.ArgumentParser(description='绘制已生成的通用场景对照时间图')
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--font', type=Path)
    args = parser.parse_args(argv)
    for path in plot_comparison(args.data, args.font):
        print(path)
    return 0
