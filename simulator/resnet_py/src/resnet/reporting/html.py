"""报告展示层：把整理后的指标渲染成 HTML 和内嵌 SVG，无第三方依赖。

输入 collect_report 返回的字典；输出 HTML 字符串。外部字符串统一转义，
曲线坐标由实际仿真时间计算。图表随案例时长伸缩，没有写死 EXT20 的时间窗。
"""
from html import escape
import json
import math

from .data import EVENT_NAMES


def e(value):
    return escape(str(value), quote=True)


def n(value, digits=2):
    return '—' if value is None else f'{value:,.{digits}f}'


DROP_NAMES = {
    'packet_loss': '链路随机丢包', 'queue_full': '队列容量不足',
    'stale_generation': '链路状态代次已变化', 'retired_epoch': '业务旧授权已退役',
    'link_down_during_serialization': '发送期间链路中断',
    'node_offline_during_serialization': '发送期间节点离线',
    'no_route': '无可用路由', 'node_offline': '节点离线',
}
MODE_NAMES = {'fixed': '固定路由', 'control': '规则带内管控', 'marl': '多智能体接口'}
PARAMS = {'bandwidthBps': '带宽 bps', 'delayMs': '配置时延 ms', 'jitterMs': '配置抖动 ms',
          'ber': '误码率配置', 'packetLossRate': '随机丢包率配置', 'state': '状态',
          'op': '操作', 'serviceType': '业务类型', 'priority': '优先级',
          'fromX': '起点 x', 'fromY': '起点 y', 'toX': '终点 x', 'toY': '终点 y'}


def table(headers, rows):
    # 单元格内容默认都视为文本；不允许事件参数中的字符成为 HTML 标签。
    return '<div class="table-wrap"><table><thead><tr>' + ''.join(
        f'<th>{e(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{e(cell)}</td>' for cell in row) + '</tr>' for row in rows
    ) + '</tbody></table></div>'


def event_detail(event):
    attrs = event['attrs']
    target = attrs.get('linkId', attrs.get('nodeId', attrs.get('serviceId', '')))
    pieces = []
    for key, value in attrs.items():
        if key in ('linkId', 'nodeId', 'serviceId'):
            continue
        if isinstance(value, dict):
            value = '；'.join(f'{PARAMS.get(k, k)}={v}' for k, v in value.items())
        pieces.append(f'{PARAMS.get(key, key)}={value}')
    return target, '；'.join(pieces)


def timeline(events, stop_s):
    if not events:
        return '<p class="muted">本轮运行范围内没有记录核心动态事件。</p>'
    kinds = list(dict.fromkeys(list(EVENT_NAMES) + [r['type'] for r in events]))
    kinds = [k for k in kinds if any(r['type'] == k for r in events)]
    left, right, width = 125, 1040, 1080
    height = 70 + 50 * len(kinds)
    x = lambda t: left + (right - left) * t / max(stop_s, 1e-9)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="实际执行的事件时间图">']
    for i, kind in enumerate(kinds):
        y = 30 + 50 * i
        out.append(f'<text x="8" y="{y+5}">{e(EVENT_NAMES.get(kind, kind))}</text>'
                   f'<line x1="{left}" y1="{y}" x2="{right}" y2="{y}" stroke="#d6e0e9"/>')
        for event in (r for r in events if r['type'] == kind):
            px = x(event['time_s'])
            target, detail = event_detail(event)
            tip = f"{event['id']} · {event['time_s']:g} s · {target} · {detail}"
            out.append(f'<circle cx="{px:.2f}" cy="{y}" r="5" fill="#256e95"><title>{e(tip)}</title></circle>')
    y = height - 35
    for i in range(6):
        t = stop_s * i / 5
        out.append(f'<text x="{x(t):.2f}" y="{y}" text-anchor="middle">{t:g}</text>')
    out.append(f'<text x="{(left+right)/2}" y="{height-8}" text-anchor="middle">仿真时间 / s</text></svg>')
    return ''.join(out)


def chart(points, key, title, unit, color, stop_s, events):
    """缺少收包的时间窗不连线，防止把无时延样本画成 0 ms。"""
    width, height = 550, 265
    left, right, top, bottom = 67, 529, 28, 212
    values = [p[key] for p in points if p[key] is not None]
    if not values:
        return f'<article><h3>{e(title)}</h3><p class="muted">本轮没有可绘制的数据。</p></article>'
    ymax = max(values) or 1
    ymax *= 1.08
    x = lambda t: left + (right-left) * t / max(stop_s, 1e-9)
    y = lambda v: bottom - (bottom-top) * v / ymax
    out = [f'<article><h3>{e(title)}</h3><svg viewBox="0 0 {width} {height}" role="img" aria-label="{e(title)}">']
    for i in range(5):
        value = ymax * i / 4
        py = y(value)
        out.append(f'<line x1="{left}" x2="{right}" y1="{py:.2f}" y2="{py:.2f}" stroke="#e5ebf0"/>'
                   f'<text x="{left-8}" y="{py+4:.2f}" text-anchor="end">{value:.3g}</text>')
    for event in events:
        px = x(event['time_s'])
        out.append(f'<line x1="{px:.2f}" x2="{px:.2f}" y1="{top}" y2="{bottom}" stroke="#a3b1bc" '
                   f'stroke-dasharray="3 5" opacity=".4"><title>{e(event["id"])} · {event["time_s"]:g} s</title></line>')
    segments, current = [], []
    for point in points:
        value = point[key]
        if value is None:
            if current:
                segments.append(current)
                current = []
        else:
            current.append((x(point['time_s']), y(value)))
    if current:
        segments.append(current)
    for segment in segments:
        if len(segment) == 1:
            px, py = segment[0]
            out.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="2" fill="{color}"/>')
        else:
            coords = ' '.join(f'{px:.2f},{py:.2f}' for px, py in segment)
            out.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>')
    for i in range(5):
        t = stop_s*i/4
        out.append(f'<text x="{x(t):.2f}" y="{bottom+22}" text-anchor="middle">{t:g}</text>')
    out.append(f'<text x="{left}" y="15">{e(unit)}</text><text x="298" y="257" text-anchor="middle">仿真时间 / s</text></svg></article>')
    return ''.join(out)


CSS = '''
:root{font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;color:#203443;background:#edf2f6;font-size:15px}
*{box-sizing:border-box}body{margin:0}main{max-width:1180px;margin:28px auto;padding:0 24px 60px}
header{padding:30px 34px;background:#15374b;color:white;border-radius:14px}h1{font-size:30px;letter-spacing:.5px;margin:8px 0 14px}
header p{color:#d8e7ef;margin:8px 0;overflow-wrap:anywhere}.eyebrow{color:#9ccee3;font-size:13px;letter-spacing:2px}
section{padding:25px 28px;background:white;margin-top:20px;border-radius:12px;border:1px solid #dfe7ed}
h2{font-size:21px;margin:0 0 16px}h3{font-size:16px;margin:0 0 12px}p,li{line-height:1.8}li{margin:6px 0}
.muted{color:#5a6e7c;font-size:13px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:18px}
.card{border:1px solid #dce6ed;border-radius:9px;padding:16px;background:#f8fbfd}.card b{font-size:25px;display:block;margin-top:10px;color:#175b79}
.badge{display:inline-block;padding:6px 12px;border-radius:5px;background:#e8f5ee;color:#216644}.bad{background:#fce9e8;color:#a03737}
.note{background:#fff7e8;border-left:4px solid #ce943e;padding:10px 16px;line-height:1.8;margin:15px 0}
.plots{display:grid;grid-template-columns:1fr 1fr;gap:20px}.plots article{min-width:0;border:1px solid #e5ebef;padding:12px;border-radius:8px}
svg{width:100%;display:block}svg text{font-family:inherit;font-size:12px;fill:#415b6b}.table-wrap{overflow:auto;margin-top:12px}
table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;background:#edf4f8;color:#284d62;padding:11px 10px;white-space:nowrap}
td{padding:10px;border-bottom:1px solid #e6edf2;vertical-align:top;line-height:1.65}tr:nth-child(even){background:#fafcfd}
details{margin:16px 0}summary{cursor:pointer;color:#256e95;padding:8px 0;font-weight:600}.source{font-family:Consolas,monospace;word-break:break-all;font-size:11px}
footer{padding:20px 4px;color:#647b8a;font-size:12px}.links a{display:inline-block;margin:5px 16px 5px 0;color:#256e95}
@media(max-width:760px){main{padding:0 12px}.cards{grid-template-columns:1fr 1fr}.plots{grid-template-columns:1fr}section{padding:20px 15px}h1{font-size:24px}}
@media print{body{background:white}main{max-width:none;margin:0;padding:0}header{background:white;color:#203443;border:1px solid #dfe7ed}header p{color:#415b6b}section{break-inside:auto;margin:12px 0}article,svg{break-inside:avoid}.table-wrap{overflow:visible}tr{break-inside:avoid}.links{display:none}}
'''


def render_report(d):
    data, cfg, perf = d['data'], d['config'], d['performance']
    mode = MODE_NAMES.get(cfg['mode'], cfg['mode'])
    # 展示名称只用于报告；目录、场景编号和运行参数不参与此映射。
    scene_titles = {
        'EXT20-02': '自建测试场景（EXT20-02）',
        'sce20261010-conf02/sce01/script-09': '官方基准场景（20节点 script-09）',
        'EXT20-03': '自建规则对齐场景（EXT20-03）',
    }
    scene_title = scene_titles.get(d['scenario_id'], d['scenario_id'] or '网络仿真')
    title = f"{scene_title} · 本次运行报告"
    scope = {True: '官方输入 / 本项目网络模型', False: '自建场景 / 本项目网络模型'}.get(
        d['dataset_official'], '来源未标注 / 本项目网络模型')
    out = [f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{e(title)}</title><style>{CSS}</style></head><body><main><header>'
           f'<div class="eyebrow">RESNET / RUN REPORT</div><h1>{e(title)}</h1>'
           f'<p>{e(mode)} · {e(d["case_id"])} · {e(scope)} · {d["node_count"]} 节点 / {d["link_count"]} 链路</p>'
           f'<p>结果目录：{e(d["run_dir"])}</p><p>报告生成时间（UTC）：{e(d["generated_at"])}</p></header>']
    purposes = {
        'EXT20-02': '自建测试场景用于验证基础网络能否跑通，通过较低负载和分阶段活动，观察业务发包、逐跳传输、排队、事件执行、带内管控及结果记录的完整流程。',
        'sce20261010-conf02/sce01/script-09': '官方基准场景使用赛事原始拓扑、40条初始业务和script-09的8条事件，建立完整官方需求下的运行参考。',
        'EXT20-03': '自建规则对齐场景是在官方场景基础上的进一步拓展：纳入全部40条初始需求，视频、图像、数据和话音各动态新增1条，并组合多种节点与链路变化，用于尽量覆盖当前样本的业务需求和动态运行条件。',
    }
    out.append('<section><h2>1　运行概况</h2>')
    if d['scenario_id'] in purposes:
        out.append(f'<p>{e(purposes[d["scenario_id"]])}</p>')
    out.append('<p>送达率衡量已生成业务包中成功到达的比例；需求完成度衡量全部有效需求得到满足的比例。管控未接纳的需求可能没有生成数据包，因此两项指标需要结合阅读。</p>')
    cards = [('仿真结束时刻 / s', n(d['stop_s'], 0)), ('实际计算耗时 / s', n(perf.get('wall_elapsed_s'))),
             ('业务送达率 / %', n(data['delivery_pct'])), ('需求按时完成度 / %', n(data['demand_timely_pct']))]
    out.append('<div class="cards">' + ''.join(f'<div class="card">{e(k)}<b>{e(v)}</b></div>' for k, v in cards) + '</div>')
    out.append(table(['项目', '本轮记录'], [
        ['业务发送时间窗', f"{cfg['start_s']:g}～{cfg['end_s']:g} s；收尾期 {cfg['drain_s']:g} s"],
        ['随机种子 / 等待队列上限', f"{cfg['seed']} / 每链路 {cfg['queue_packets']} 包，{cfg['queue_bytes']} B"],
        ['核心事件 / 批次', f"{len(d['events'])} 条 / {len(d['batches'])} 批"],
        ['采样记录', f"{d['telemetry']['samples']} 条" if d['telemetry'] else '无独立网络状态采样'],
        ['控制器处理累计耗时 / s', n(perf.get('decision_wall_time_ns', 0) / 1e9, 4)],
    ]))
    out.append('<ul>' + ''.join(f'<li>{e(s)}</li>' for s in d['assessments']) + '</ul>')
    for warning in d['warnings']:
        out.append(f'<div class="note">{e(warning)}</div>')
    # 数据来源和决策规则分别说明：扩展场景也可使用官方配置质量公式。
    if d['dataset_official'] is True:
        out.append('<p class="note">原始拓扑、业务和事件来自官方场景；运行终点、分组/队列模型及位置更新执行方式属于本项目运行约定。</p>')
    elif d['dataset_official'] is False:
        out.append('<p class="note">本轮使用项目扩展场景；事件参数与时间安排的依据见场景来源说明。运行终点、分组/队列模型及位置更新执行方式属于本项目运行约定。</p>')
    else:
        out.append('<p class="note">本轮结果未标注场景数据来源；请结合输入文件和场景说明确认。</p>')
    out.append('<p class="note">需求按时完成度只评价各业务的时延门限；实测带宽、时延、抖动、丢包率和误码率是否同时达标需另行评价。离线或未接纳期间的活动需求仍计入完成度分母。</p>')
    if cfg['mode'] == 'control':
        # summary.config 保存本轮输入配置；缺省值与 control_defaults 一致。
        qos_profile = cfg.get('control', {}).get('qos_profile', 'baseline')
        if qos_profile == 'official':
            out.append('<p class="muted">规则管控先接纳再发送，使用官方配置QoS合成口径：时延求和、抖动平方和开方、丢包率独立合成、误码率近似求和。容量可行的最小时延候选超限时拒绝；当前基线不穷举其他QoS可行路径。实际传输仍包含串行发送、排队与丢包。</p>')
        else:
            out.append('<p class="muted">规则管控先接纳再发送，使用原基线质量模式：路径时延估计包含传播与串行发送耗时，配置误码率取各链路最大值。质量超限会记录原因，容量可行的候选仍可接纳。</p>')
    elif cfg['mode'] == 'fixed':
        out.append('<p class="muted">固定路由使用预先生成的路径，按可发送需求发包；运行期不执行规则管控的业务接纳和候选路径质量拒绝。</p>')
    out.append('</section><section><h2>2　业务交付与丢弃</h2>')
    out.append(table(['统计项', '本次结果', '计算口径'], [
        ['生成 / 送达 / 丢弃 / 在途', ' / '.join(n(data[k], 0) for k in ('generated','delivered','dropped','pending')), '按业务包计数'],
        ['业务送达率', n(data['delivery_pct']) + '%', '送达包数 / 生成包数'],
        ['业务丢弃率', n(data['loss_pct']) + '%', '终结丢弃包数 / 生成包数；未生成的需求另行评价'],
        ['需求交付完成度', n(data['demand_delivery_pct']) + '%', '送达负载 bit / 活动需求 bit'],
        ['需求按时完成度', n(data['demand_timely_pct']) + '%', '按各业务时延门限送达的负载 bit / 活动需求 bit'],
        ['已收包按时比例', n(data['received_timely_pct']) + '%', '按时送达包数 / 已送达包数'],
        ['已收包平均 / 最大时延', n(data['mean_delay_ms']) + ' / ' + n(data['max_delay_ms']) + ' ms', '包生成至接收；未送达包不进入时延均值'],
    ]))
    reasons = sorted(data['drop_reasons'].items(), key=lambda p: (-p[1], p[0]))
    out.append(table(['丢弃原因', '日志字段', '包数'], [[DROP_NAMES.get(k,k),k,n(v,0)] for k,v in reasons]) if reasons else '<p>本轮没有记录业务终结丢弃。</p>')
    out.append('</section><section><h2>3　网络变化与事件时间轴</h2>')
    out.append('<p class="muted">圆点表示本轮实际执行的核心事件；悬停可查看时刻与参数。横轴均为相对 T0 的仿真秒数。</p>')
    out.append(timeline(d['events'], d['stop_s']))
    out.append(f'<p class="muted">以下曲线按 {n(d["series"]["window_s"],0)} 秒窗口汇总；末窗使用实际时长。虚线对应核心事件时刻。排队图显示窗口内采样到的全网等待队列总包数最大值。</p><div class="plots">')
    for key, label, unit, color in [('goodput_mbps','业务交付速率','Mbps','#147d70'),
                                   ('delay_ms','已收包平均时延','ms','#b67721'),
                                   ('dropped','窗口内业务丢弃','包','#ba5151'),
                                   ('sampled_queue_max','全网等待队列采样峰值','包','#286f9b')]:
        out.append(chart(d['series']['points'],key,label,unit,color,d['stop_s'],d['events']))
    out.append('</div><details open><summary>事件时刻与实际更新参数</summary>')
    out.append(table(['编号','仿真时间 / s','事件','对象','更新内容'], [
        [r['id'],n(r['time_s'],3),EVENT_NAMES.get(r['type'],r['type']),*event_detail(r)] for r in d['events']]))
    out.append('</details><details><summary>全部场景批次与业务活动状态</summary>')
    out.append(table(['批次','仿真时间 / s','业务剖面','活动业务数','在线节点','可用链路'], [
        [r['sequence'],n(r['time_s'],3),r['profile'] or '—',r['active_services'],r['online_nodes'],r['available_links']] for r in d['batches']]))
    out.append('</details></section><section><h2>4　逐业务与逐链路结果</h2>')
    headers = ['业务','类型 / 端点','需求交付 / 按时完成 %','生成 / 送达 / 丢弃','平均时延 ms']
    def service_row(r):
        return [r['id'],f"{r['type']} · {r['source']} → {r['destination']}",
                n(r['demand_delivery_pct'])+' / '+n(r['demand_timely_pct']),
                ' / '.join(n(r[k],0) for k in ('generated','delivered','dropped')),n(r['mean_delay_ms'])]
    out.append('<p class="muted">需求来自本轮活动时间内的速率积分；停源、断链和节点离线期间的需求保留在分母中。表中的“—”表示没有适用样本。</p>')
    out.append(table(headers,[service_row(r) for r in d['services'] if r['demand_bits'] > 0]))
    out.append('<details><summary>本轮无活动需求的登记业务</summary>')
    out.append(table(headers,[service_row(r) for r in d['services'] if r['demand_bits'] == 0]))
    out.append('</details><details open><summary>链路运行统计</summary>')
    out.append('<p class="muted">容量与通断为运行终态；忙时占比为全程序列化忙时 / 仿真结束时刻，包含收尾期。排队峰值来自实际入队计数，区别于上方的定期采样峰值。链路统计同时包含业务和管控流量。</p>')
    out.append(table(['链路 / 端点','终态 / 容量 Mbps','忙时占比 %','队列峰值 / 上限','平均 / 最大排队 ms','链路丢弃'], [
        [r['id']+' · '+' ↔ '.join(r['endpoints']),('up' if r['up'] else 'down')+' / '+n(r['capacity_bps']/1e6,3),
         n(r['busy_pct']),f"{r['queue_peak']} / {r['queue_limit']}",n(r['mean_queue_ms'])+' / '+n(r['max_queue_ms']),n(r['drops'],0)] for r in d['links']]))
    out.append('</details></section><section><h2>5　管控过程与数据来源</h2>')
    c = d['control']
    out.append(table(['管控报文统计','本轮结果'],[
        ['生成 / 送达 / 丢弃 / 在途', ' / '.join(n(c[k],0) for k in ('generated','delivered','dropped','pending'))],
        ['已收管控报文平均时延',n(c['mean_delay_ms'])+' ms'],
    ]))
    details = d['control_details']
    if details:
        out.append(table(['命令执行状态','次数'], [[k,v] for k,v in details.get('execution_statuses',{}).items()]))
        out.append(table(['执行结果原因','次数'], [[k,v] for k,v in details.get('execution_reasons',{}).items()]))
        out.append('<p class="muted">执行状态统计的是节点对命令的处理结果；单次 applied 不直接代表业务已经恢复。</p>')
    out.append('<details><summary>本次报告的数据来源与文件摘要</summary><p class="muted">报告仅读取本次结果目录，不读取其他运行结果或预先编写的分析结论。SHA-256 用于确认对应的原始文件。</p>')
    out.append(table(['文件','大小 / B','SHA-256'],[[k,v['bytes'],v['sha256']] for k,v in d['evidence'].items()]))
    out.append('</details><p class="links"><a href="summary.json">原始汇总</a><a href="services.csv">业务统计表</a><a href="report-data.json">报告计算数据</a></p>')
    out.append('<p class="muted">模型保留有限队列、共享链路容量和动态事件；无线物理层已抽象，BER 为配置元数据。事件与曲线的时间对应便于核查，单次运行的变化仍需结合路径和管控日志解释。</p></section>')
    out.append('<footer>本报告由本轮结果自动生成。HTML 内含全部图表，可离线打开或通过浏览器打印为 PDF。</footer></main></body></html>')
    return ''.join(out)
