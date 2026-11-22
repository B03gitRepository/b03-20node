"""把本次运行的结果文件整理成可离线打开的报告，不再次执行网络仿真。"""
from pathlib import Path
import json

from .data import collect_report
from .html import render_report


def generate_report(run_dir):
    """输入结果目录；输出 report.html，同时保存可复查的 report-data.json。

    只读取该目录中的运行证据，不读取当前场景文件，避免修改剧本后误用新参数。
    所有图表嵌入同一个 HTML 文件，其他电脑用浏览器打开也无需安装绘图库。
    """
    root = Path(run_dir).resolve()
    data = collect_report(root)
    html = render_report(data)
    # 先写临时文件，再替换报告；不修改 summary、日志或已有实验数据。
    for name, content in (
        ('report-data.json', json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'),
        ('report.html', html),
    ):
        target = root / name
        temp = root / (name + '.tmp')
        temp.write_text(content, encoding='utf-8')
        temp.replace(target)
    return root / 'report.html'
