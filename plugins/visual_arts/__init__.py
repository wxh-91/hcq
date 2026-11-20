# -*- coding: utf-8 -*-
"""Visual Arts Toolkit 插件 —— 数学与几何绘图工具箱。

对外 AUP 工具：
  render_python_code(code, filename)    [宏]   执行 Python 绘图代码 → 生成图片 → 自动飞书发送
  draw_math_functions(functions, x_range, filename)  [原子] 数学函数图像（正比例/反比例/二次函数等）
  draw_geometric_shapes(shapes, filename)  [宏]  基础几何图形组合渲染（如用椭圆+三角形拼"蓝鲸"）→ 自动发送

依赖：matplotlib（headless agg backend）、PIL。
坐标约定：数学坐标（x 向右，y 向上），matplotlib 渲染，隐藏坐标轴。
"""
from __future__ import annotations
import os
_ROOT = os.environ.get("PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import io
import json
import logging
import math
import os
import subprocess
import sys

from aup_core import aup_tool


logger = logging.getLogger("plugins.visual_arts")

VENV_PY = _ROOT + "/venv310/bin/python"
OUT_DIR = _ROOT + "/visual_arts_tmp"

try:
    import matplotlib
    matplotlib.use("Agg")  # headless 必须
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse, FancyBboxPatch, Polygon, Rectangle, Circle
    from matplotlib import colors as mcolors
    MPL_OK = True
except Exception as _e:  # 运行时再给可读错误
    MPL_OK = False
    logger.warning("matplotlib 不可用: %s", type(_e).__name__)

try:
    from PIL import Image
    PIL_OK = True
except Exception:
    PIL_OK = False


def _ensure_out_dir():
    os.makedirs(OUT_DIR, exist_ok=True)


def _resolve_out(filename: str) -> str:
    """filename 可以是绝对路径或相对路径，统一落到 OUT_DIR。"""
    if os.path.isabs(filename):
        return filename
    return os.path.join(OUT_DIR, os.path.basename(filename))


def _is_image(path: str) -> bool:
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 工具 1：render_python_code（宏）
# ---------------------------------------------------------------------------
@aup_tool(
    name="render_python_code",
    description=(
        "[宏] 执行 Python 绘图代码并发送图片：传入一段 Python 代码（使用 matplotlib 或 PIL），"
        "工具内部在隔离子进程执行，代码中必须把图片保存为参数 filename 指定的文件名"
        "（相对路径即可，工具会自动定位输出目录），执行成功后自动调用 feishu_send_image 发送给用户。"
        "适用于 AI 用代码绘制任意图像（如 turtle/matplotlib 风格的自定义图形）。"
        "若代码报错，工具会返回完整报错信息，AI 应修正代码后重试。"
    ),
    category="visual",
    is_danger=False,
    params_schema={
        "required": ["code", "filename"],
        "properties": {
            "code": {"type": "string", "description": "Python 绘图代码（用 matplotlib 或 PIL），代码内必须把最终图片保存为 filename 指定的文件名，如 plt.savefig(filename) 或 img.save(filename)"},
            "filename": {"type": "string", "description": "输出图片文件名，如 whale.png（相对路径即可，工具自动定位到 /root/robot/visual_arts_tmp/）"},
        },
    },
    suggestion_hint="代码报错时把报错返回给 AI 让其修正；输出路径始终以 /root/robot/visual_arts_tmp/ 为准",
)
async def render_python_code(code: str, filename: str) -> str:
    _ensure_out_dir()
    fname = os.path.basename(filename)
    out_path = _resolve_out(fname)
    # 输出目录软链接注入：代码里 plt.savefig(filename) 会落在 cwd；注入 filename 变量供代码引用
    script = "# -*- coding: utf-8 -*-\nimport sys\nsys.path.insert(0, '/root/robot')\nfilename = %r\n" % fname + code
    tmp_py = os.path.join(OUT_DIR, "_render_" + str(os.getpid()) + ".py")
    try:
        with open(tmp_py, "w", encoding="utf-8") as f:
            f.write(script)
    except Exception as e:
        return "❌ 无法写入临时脚本：%s" % type(e).__name__
    try:
        proc = subprocess.run(
            [VENV_PY, tmp_py],
            cwd=OUT_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "❌ 代码执行超时（120 秒），已终止。请检查是否有死循环或过重计算。"
    finally:
        try:
            os.remove(tmp_py)
        except OSError:
            pass
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "未知错误").strip()
        return "❌ 代码执行失败（exit=%s），报错信息如下，请修正代码后重试：\n%s" % (proc.returncode, err[-2000:])
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return "❌ 代码执行成功但未在 %s 生成图片。请确认代码中把图片保存为 filename（例如 plt.savefig('%s')）" % (out_path, fname)
    if not _is_image(out_path):
        return "❌ %s 不是有效图片文件。请确认代码输出的是 PNG/JPG 图片。" % out_path
    # 发送
    try:
        from plugins.feishu_delivery import feishu_send_image
        sent = await feishu_send_image(out_path)
    except Exception as e:
        return "✅ 图片已生成（%s），但自动发送失败：%s" % (out_path, type(e).__name__)
    return "✅ 图片已生成并发送：%s（%s）" % (out_path, sent)


# ---------------------------------------------------------------------------
# 工具 2：draw_math_functions（原子）
# ---------------------------------------------------------------------------
@aup_tool(
    name="draw_math_functions",
    description=(
        "[原子] 数学函数图像绘制：传入函数表达式列表（如 [\"x**2\", \"2*x\", \"1/x\"]，隐式 y=），"
        "和 x 取值范围（如 [-10, 10]），生成一张函数图像并返回图片路径。"
        "支持正比例、反比例、二次函数、三角函数等任意数学表达式。"
        "返回的是本地图片路径，如需发送给用户请再调用 feishu_send_image。"
    ),
    category="visual",
    is_danger=False,
    params_schema={
        "required": ["functions", "x_range"],
        "properties": {
            "functions": {"type": "array", "description": "函数表达式列表，如 [\"x**2\", \"2*x\", \"1/x\", \"sin(x)\"]，表达式内可直接使用 x 与 math 函数（sin/cos/sqrt/abs 等）"},
            "x_range": {"type": "array", "description": "x 取值区间 [xmin, xmax]，如 [-10, 10]"},
            "filename": {"type": "string", "description": "输出文件名，如 functions.png（可选，默认 auto.png）"},
        },
    },
    suggestion_hint="表达式用 eval 安全命名空间解析，非法表达式会返回明确错误",
)
async def draw_math_functions(functions: list, x_range: list, filename: str = "functions.png") -> str:
    if not MPL_OK:
        return "❌ matplotlib 未安装，无法绘图。请先安装 matplotlib。"
    try:
        xmin, xmax = float(x_range[0]), float(x_range[1])
    except Exception:
        return "❌ x_range 必须是 [xmin, xmax] 两个数字"
    if xmax <= xmin:
        return "❌ x_range 非法：xmax 必须大于 xmin"
    _ensure_out_dir()
    out_path = _resolve_out(filename)
    safe_ns = {"__builtins__": {}}
    safe_ns.update({k: v for k, v in vars(math).items() if not k.startswith("_")})
    try:
        fig, ax = plt.subplots(figsize=(9, 6), dpi=110)
        N = 2000
        xs = [xmin + (xmax - xmin) * i / N for i in range(N + 1)]
        for expr in functions:
            if not isinstance(expr, str) or not expr.strip():
                continue
            fn = expr.strip()
            ys = []
            for xv in xs:
                try:
                    yv = eval(fn, safe_ns, {"x": xv})
                    ys.append(yv if isinstance(yv, (int, float)) and yv == yv and abs(yv) < 1e9 else None)
                except Exception:
                    ys.append(None)
            xv_ok, yv_ok = [], []
            for xv, yv in zip(xs, ys):
                if yv is not None:
                    xv_ok.append(xv)
                    yv_ok.append(yv)
            if xv_ok:
                ax.plot(xv_ok, yv_ok, linewidth=2.0, label=f"y = {fn}")
        ax.axhline(0, color="black", linewidth=0.8)
        ax.axvline(0, color="black", linewidth=0.8)
        ax.grid(True, linestyle="--", alpha=0.4)
        ax.set_xlim(xmin, xmax)
        ax.legend(loc="best", fontsize=11)
        ax.set_title("Math Functions", fontsize=14)
        fig.tight_layout()
        fig.savefig(out_path, format="png")
        plt.close(fig)
    except Exception as e:
        return "❌ 绘图失败：%s" % str(e)[-1000:]
    return "✅ 图片已生成：%s（函数数=%d，x∈[%s, %s]）。如需发送请调用 feishu_send_image。" % (
        out_path, len(functions), xmin, xmax)


# ---------------------------------------------------------------------------
# 工具 3：draw_geometric_shapes（宏）
# ---------------------------------------------------------------------------
@aup_tool(
    name="draw_geometric_shapes",
    description=(
        "[宏] 基础几何图形组合渲染：传入图形 JSON 数组，工具用 matplotlib 把椭圆/圆/矩形/三角形/多边形/线段"
        "按坐标组合渲染成一张图，并自动调用 feishu_send_image 发送给用户。"
        "适用于用基础几何形状拼出具体物体（如用椭圆+三角形组合画鲸鱼）。"
        "支持图形类型：ellipse(center,radius,color)、circle(center,r,color)、rectangle(center,width,height,color)、"
        "triangle(points,color)、polygon(points,color)、line(points,color)。"
        "颜色支持颜色名（blue/black/...）或 #RRGGBB。"
    ),
    category="visual",
    is_danger=False,
    params_schema={
        "required": ["shapes"],
        "properties": {
            "shapes": {"type": "array", "description": "图形数组，每项含 type 与坐标参数，示例：[{\"type\":\"ellipse\",\"center\":[0,0],\"radius\":[10,5],\"color\":\"blue\"},{\"type\":\"triangle\",\"points\":[[10,0],[15,5],[10,-5]],\"color\":\"blue\"}]"},
            "filename": {"type": "string", "description": "输出文件名，如 whale.png（可选，默认 shapes.png）"},
        },
    },
    suggestion_hint="坐标用数学坐标；图形超出画布时自动扩展；无效图形类型返回明确错误",
)
async def draw_geometric_shapes(shapes: list, filename: str = "shapes.png") -> str:
    if not MPL_OK:
        return "❌ matplotlib 未安装，无法绘图。请先安装 matplotlib。"
    if isinstance(shapes, str):
        try:
            shapes = json.loads(shapes)
        except Exception:
            return "❌ shapes 传参为字符串但 JSON 解析失败，请直接传数组：shapes=[{\"type\":\"ellipse\",...}]"
    if not isinstance(shapes, list) or not shapes:
        return "❌ shapes 必须是包含至少一个图形的数组"
    _ensure_out_dir()
    out_path = _resolve_out(filename)
    # 收集所有坐标点以自动适配画布范围
    all_pts = []
    parsed = []
    for idx, s in enumerate(shapes):
        if not isinstance(s, dict):
            return "❌ 第 %d 个图形必须是对象" % (idx + 1)
        t = str(s.get("type", "")).lower()
        color = s.get("color", "blue")
        try:
            rgba = mcolors.to_rgba(color)
        except Exception:
            return "❌ 第 %d 个图形颜色无效：%r" % (idx + 1, color)
        # [Edge Fix] 防呆：缺 key / 类型错 / 旧 schema 兼容 → 标准 AUPResult 风格报错（含正确传参示例）
        try:
            if t in ("ellipse", "circle"):
                center = s.get("center")
                if not isinstance(center, (list, tuple)) or len(center) < 2:
                    return "❌ 第 %d 个图形（%s）缺少 center，请传 [cx,cy] 坐标数组，如 \"center\":[0,0]" % (idx + 1, t)
                if t == "circle":
                    rx = ry = float(s.get("r", s.get("radius", 1)))
                else:
                    r = s.get("radius")
                    if r is None and ("width" in s or "height" in s):
                        # 兼容旧 width/height 写法（radius = 半宽/半高）
                        _w = float(s.get("width", 0))
                        _h = float(s.get("height", _w))
                        rx, ry = _w / 2.0, _h / 2.0
                    elif r is None:
                        return "❌ 第 %d 个图形（ellipse）缺少 radius，请传 [rx,ry]（或 width/height 旧写法），如 \"radius\":[10,5]" % (idx + 1)
                    else:
                        rx, ry = float(r[0]), float(r[1])
                parsed.append(("ellipse", center, (rx, ry), rgba))
                all_pts.append(center)
                all_pts.append([center[0] + rx, center[1] + ry])
                all_pts.append([center[0] - rx, center[1] - ry])
            elif t in ("rectangle", "rect", "square"):
                center = s.get("center")
                if not isinstance(center, (list, tuple)) or len(center) < 2:
                    return "❌ 第 %d 个图形（%s）缺少 center，请传 [cx,cy] 坐标数组，如 \"center\":[0,0]" % (idx + 1, t)
                w = float(s.get("width", s.get("size", 1)))
                h = float(s.get("height", s.get("size", w)))
                parsed.append(("rect", center, (w, h), rgba))
                all_pts.append(center)
                all_pts.append([center[0] + w / 2, center[1] + h / 2])
                all_pts.append([center[0] - w / 2, center[1] - h / 2])
            elif t in ("triangle", "polygon", "line"):
                pts = s.get("points")
                if not pts:
                    return "❌ 第 %d 个图形（%s）缺少 points，请传坐标数组 [[x1,y1],[x2,y2],...]" % (idx + 1, t)
                pts = [[float(p[0]), float(p[1])] for p in pts]
                if t == "line":
                    parsed.append(("line", pts, None, rgba))
                else:
                    parsed.append(("polygon", pts, None, rgba))
                all_pts.extend(pts)
            else:
                return "❌ 第 %d 个图形类型不支持：%r（支持 ellipse/circle/rectangle/triangle/polygon/line）" % (idx + 1, t)
        except (KeyError, TypeError, ValueError, IndexError) as _e:
            return ("❌ 第 %d 个图形（%s）参数格式错误：%s。正确示例：[{\"type\":\"ellipse\",\"center\":[0,0],"
                    "\"radius\":[10,5],\"color\":\"blue\"},{\"type\":\"triangle\",\"points\":[[10,0],[15,5],[10,-5]],\"color\":\"blue\"}]" % (idx + 1, t, _e))
    if not all_pts:
        return "❌ 无法解析图形坐标"
    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    pad_x = max((max(xs) - min(xs)) * 0.1, 1.0)
    pad_y = max((max(ys) - min(ys)) * 0.1, 1.0)
    try:
        fig, ax = plt.subplots(figsize=(9, 6), dpi=110)
        for kind, a, b, rgba in parsed:
            if kind == "ellipse":
                ax.add_patch(Ellipse(xy=a, width=b[0] * 2, height=b[1] * 2, color=rgba, alpha=0.9))
            elif kind == "rect":
                cx, cy = a
                w, h = b
                ax.add_patch(Rectangle((cx - w / 2, cy - h / 2), w, h, color=rgba, alpha=0.9))
            elif kind == "polygon":
                ax.add_patch(Polygon(a, closed=True, color=rgba, alpha=0.9))
            elif kind == "line":
                ax.plot([p[0] for p in a], [p[1] for p in a], color=rgba, linewidth=3)
        ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
        ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
        ax.set_aspect("equal")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(out_path, format="png", facecolor="white")
        plt.close(fig)
    except Exception as e:
        return "❌ 渲染失败：%s" % str(e)[-1000:]
    try:
        from plugins.feishu_delivery import feishu_send_image
        sent = await feishu_send_image(out_path)
    except Exception as e:
        return "✅ 图片已生成（%s），但自动发送失败：%s" % (out_path, type(e).__name__)
    return "✅ 图片已生成并发送：%s（%s）" % (out_path, sent)


# ---------------------------------------------------------------------------
# 显式注册（幂等 upsert；@aup_tool 已自动注册，此处双保险）
# ---------------------------------------------------------------------------
def _register():
    try:
        from tool_center.registry_manager import register_tool
        for fn in (render_python_code, draw_math_functions, draw_geometric_shapes):
            try:
                register_tool(name=fn.__name__, func=fn, category="visual")
                logger.info("[visual_arts] 注册成功: %s", fn.__name__)
            except Exception as e:
                logger.warning("[visual_arts] 注册 %s 失败: %s", fn.__name__, type(e).__name__)
    except Exception as e:
        logger.warning("[visual_arts] registry 不可用: %s", type(e).__name__)


_register()
