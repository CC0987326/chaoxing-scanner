# -*- coding: utf-8 -*-
"""
学习通登录页「点选汉字」验证码处理
------------------------------------------------------------------
学习通的安全验证是 canvas 绘制的：提示「请依次点击『清』『应』『直』」，
要求按顺序点掉图里对应的三个汉字。

处理策略（两段式，兼顾省事和可靠）：
  第 1 段 · 自动识别
      ddddocr 检测文字框 → 逐个识别 → 按提示顺序建立「字→位置」映射。
      实测这类书法体汉字识别率约 1/2 ~ 2/3，敢用但不敢全信，
      所以要求「三个字全部识别成功」才自动点击，否则进第 2 段。
  第 2 段 · 编号兜底
      把检测到的候选框编号画在图上并自动打开，
      你只需按提示顺序输入编号（例如「3 1 4」），程序替你点击。
      大约 5 秒，命中率 100%。

离线自测：
  python captcha_solver.py <截图.png> [画布left,画布top,宽,高]
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
    except Exception:
        pass

HAS_DDDDOCR = False
try:
    import ddddocr  # noqa

    HAS_DDDDOCR = True
except Exception:
    pass

HERE = Path(__file__).resolve().parent
SHOT_DIR = HERE / 'runtime' / 'screenshots'

_DET = None
_CLS = None


def _models():
    """懒加载 OCR 模型（首次调用约 1 秒）"""
    global _DET, _CLS
    if _DET is None:
        _DET = ddddocr.DdddOcr(det=True, show_ad=False)
        try:
            _CLS = ddddocr.DdddOcr(det=False, show_ad=False, beta=True)
        except TypeError:
            _CLS = ddddocr.DdddOcr(det=False, show_ad=False)
    return _DET, _CLS


def locate_chars(image_bytes: bytes, sort=True):
    """检测图中的字符框并识别内容。

    返回 [{index, char, center(x,y), box(x0,y0,x1,y1)}]，编号从 1 开始，按阅读顺序排列。
    """
    if not HAS_DDDDOCR:
        return []
    try:
        from PIL import Image
    except Exception:
        return []
    det, cls = _models()
    try:
        boxes = det.detection(image_bytes) or []
    except Exception:
        return []
    im = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    out = []
    for b in boxes:
        try:
            x0, y0, x1, y1 = [int(v) for v in b[:4]]
        except Exception:
            continue
        crop = im.crop((x0, y0, x1, y1))
        buf = io.BytesIO()
        crop.save(buf, format='PNG')
        try:
            ch = (cls.classification(buf.getvalue()) or '').strip()
        except Exception:
            ch = ''
        out.append({'char': ch, 'center': ((x0 + x1) // 2, (y0 + y1) // 2),
                    'box': (x0, y0, x1, y1)})
    if sort:
        out.sort(key=lambda f: (f['box'][1] // 12, f['box'][0]))
    for i, f in enumerate(out, 1):
        f['index'] = i
    return out


def segment_by_color(image_bytes: bytes, min_area: int = 90):
    """兜底定位：按颜色做连通域分割（只给位置，不给字），供调试查看"""
    try:
        from PIL import Image
        from collections import deque, Counter
    except Exception:
        return []
    im = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    px = im.load()
    W, H = im.size

    def cls(r, g, b):
        if min(r, g, b) > 200:
            return 'white'
        if g > 130 and g - r > 30 and g - b > 45:
            return 'green'
        if b > 130 and b - r > 35 and g > 110:
            return 'cyan'
        if r > 160 and g > 140 and b < 150 and (r + g) / 2 - b > 50:
            return 'yellow'
        if r > 120 and r - g > 50 and r - b > 50:
            return 'red'
        return None

    lab = [[cls(*px[x, y]) for x in range(W)] for y in range(H)]
    seen = [[False] * W for _ in range(H)]
    res = []
    for y in range(H):
        for x in range(W):
            if not lab[y][x] or seen[y][x]:
                continue
            q = deque([(x, y)])
            seen[y][x] = True
            pts = []
            while q:
                cx, cy = q.popleft()
                pts.append((cx, cy))
                for dx in (-2, -1, 0, 1, 2):
                    for dy in (-2, -1, 0, 1, 2):
                        nx, ny = cx + dx, cy + dy
                        if 0 <= nx < W and 0 <= ny < H and lab[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            q.append((nx, ny))
            if len(pts) >= min_area:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                cnt = Counter(lab[p[1]][p[0]] for p in pts)
                res.append({'area': len(pts),
                            'center': (sum(xs) // len(xs), sum(ys) // len(ys)),
                            'box': (min(xs), min(ys), max(xs), max(ys)),
                            'color': cnt.most_common(1)[0][0]})
    res.sort(key=lambda t: -t['area'])
    return res


# ---------------------------------------------------------------- 页面交互
JS_READ_TIP = r"""
() => {
  const b = document.body ? (document.body.innerText || '') : '';
  let m = b.match(/请依次点击[^\n]*/);
  if (!m) m = b.match(/请依次选出[^\n]*|请点击[^\n]{1,20}验证/);
  const modal = document.querySelector('.cx_comImageValidate');
  return {
    tip: m ? m[0] : '',
    hasModal: !!modal,
    modalBox: modal ? (function (r) {
      return [r.x, r.y, r.width, r.height];
    })(modal.getBoundingClientRect()) : null
  };
}
"""

JS_CANVAS_BOX = r"""
() => {
  const c = document.querySelector('.cx_comImageValidate canvas') ||
            document.querySelector('canvas');
  if (!c) return null;
  const b = c.getBoundingClientRect();
  return [b.x, b.y, b.width, b.height, c.width, c.height];
}
"""


def read_targets(page):
    """读出提示中的目标字与顺序；没有验证码弹窗返回 None"""
    try:
        info = page.evaluate(JS_READ_TIP) or {}
    except Exception:
        return None
    if not info.get('hasModal'):
        return None
    tip = info.get('tip') or ''
    chars = re.findall(r'[「『“"]([\u4e00-\u9fa5])[」』”"]', tip)
    if not chars:
        noise = set('请依次点击选出下图中文字并完成验证')
        chars = [c for c in re.findall(r'[\u4e00-\u9fa5]', tip) if c not in noise]
    return {'chars': chars[:6], 'tip': tip, 'modalBox': info.get('modalBox')}


def canvas_screenshot(page) -> bytes:
    for sel in ('.cx_comImageValidate canvas', 'canvas'):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            return loc.screenshot()
        except Exception:
            continue
    return b''


def canvas_rect(page):
    try:
        box = page.evaluate(JS_CANVAS_BOX)
    except Exception:
        box = None
    if not box:
        return None
    ox, oy, cw, ch, nw, nh = box
    return ox, oy, (cw / nw if nw else 1.0), (ch / nh if nh else 1.0)


def annotate(image_bytes: bytes, found, out_name='captcha_annotated.png') -> Path:
    """把候选框编号画在放大图上，方便人工指定顺序"""
    from PIL import Image, ImageDraw
    try:
        from PIL import ImageFont
        font = ImageFont.truetype('arial.ttf', 26)
    except Exception:
        font = None
    im = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    S = 3
    big = im.resize((im.width * S, im.height * S), Image.LANCZOS)
    d = ImageDraw.Draw(big)
    for f in found:
        x0, y0, x1, y1 = [v * S for v in f['box']]
        d.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=3)
        label = '%d  %s' % (f['index'], f['char'] or '?')
        tw = 18 * len(label)
        d.rectangle([x0, max(0, y0 - 30), x0 + tw, y0], fill=(255, 255, 0))
        d.text((x0 + 3, max(0, y0 - 28)), label, fill=(200, 0, 0), font=font)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / out_name
    big.save(path)
    return path


def _click_sequence(page, points, rect):
    ox, oy, sx, sy = rect
    for (px, py) in points:
        x = ox + px * sx
        y = oy + py * sy
        page.mouse.move(x, y)
        page.wait_for_timeout(300)
        page.mouse.down()
        page.wait_for_timeout(120)
        page.mouse.up()
        page.wait_for_timeout(900)


def solve_point_captcha(page, interactive=True, debug=False, max_rounds=3) -> bool:
    """识别并点击点选验证码。成功返回 True"""
    if not HAS_DDDDOCR:
        print('[验证码] 未安装 ddddocr，无法自动处理 → 请改用可见窗口手动登录')
        return False

    for rnd in range(1, max_rounds + 1):
        page.wait_for_timeout(1500)
        info = read_targets(page)
        if not info:
            print('[验证码] 当前没有点选验证码弹窗')
            return False
        targets = info['chars']
        print('[验证码] 第 %d 轮 · 提示：%s' % (rnd, info['tip'] or '(未读到提示)'))

        shot = canvas_screenshot(page)
        rect = canvas_rect(page)
        if not shot or not rect:
            print('[验证码] 取验证码画布失败')
            return False
        if debug:
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            (SHOT_DIR / 'captcha_canvas.png').write_bytes(shot)

        found = locate_chars(shot)
        print('[验证码] 检测到 %d 个候选框：%s'
              % (len(found), ' '.join('%d=%s' % (f['index'], f['char'] or '?') for f in found)))

        plan = []
        # ---- 第 1 段：自动识别（要求目标字全部命中）
        if targets and found:
            used, ok = set(), True
            for ch in targets:
                hit = next((f for f in found
                            if f['index'] not in used and f['char'] and
                            (f['char'] == ch or ch in f['char'])), None)
                if hit is None:
                    ok = False
                    break
                used.add(hit['index'])
                plan.append(hit['center'])
            if ok and len(plan) == len(targets):
                print('[验证码] 自动识别成功：%s → %s'
                      % (' '.join(targets), [tuple(p) for p in plan]))
            else:
                print('[验证码] 自动识别未全部命中，转人工指定')
                plan = []
        else:
            plan = []

        # ---- 第 2 段：编号兜底
        if not plan:
            if not found:
                print('[验证码] 没能检出任何候选框')
                return False
            if not interactive:
                print('[验证码] 非交互环境，放弃')
                return False
            path = annotate(shot, found)
            print('[验证码] 已生成标注图：%s' % path)
            try:
                import os
                os.startfile(str(path))  # type: ignore[attr-defined]
                print('[验证码] 已自动打开，看图里红框上的编号')
            except Exception:
                print('[验证码] 请手动打开上面这个文件查看红框编号')
            if info['tip']:
                print('[验证码] 提示顺序：%s' % info['tip'])
            try:
                raw = input('[验证码] 按提示顺序输入红框编号（如 3 1 4），直接回车放弃：').strip()
            except EOFError:
                return False
            idx = [int(v) for v in re.findall(r'\d+', raw)]
            idx = [i for i in idx if 1 <= i <= len(found)]
            if not idx:
                print('[验证码] 已放弃')
                return False
            plan = [next(f['center'] for f in found if f['index'] == i) for i in idx]

        _click_sequence(page, plan, rect)
        page.wait_for_timeout(2500)
        if not read_targets(page):
            print('[验证码] 通过 ✓')
            return True
        print('[验证码] 未通过，重试 …')
        try:
            from PIL import Image
            Image.open(io.BytesIO(canvas_screenshot(page))).close()
        except Exception:
            pass
    return False


# ---------------------------------------------------------------- 离线自测
def _selftest(path, box=None):
    data = open(path, 'rb').read()
    if box:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert('RGB')
        x, y, w, h = box
        buf = io.BytesIO()
        im.crop((x, y, x + w, y + h)).save(buf, format='PNG')
        data = buf.getvalue()
    print('dddocr 可用：', HAS_DDDDOCR)
    found = locate_chars(data)
    print('--- 检测与识别（%d 个）---' % len(found))
    for f in found:
        print('   #%d  %-3s  center=%s  box=%s'
              % (f['index'], f['char'] or '?', f['center'], f['box']))
    if found:
        print('--- 标注图 ---')
        print('   %s' % annotate(data, found, 'selftest_annotated.png'))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    _selftest(sys.argv[1],
              [int(v) for v in sys.argv[2].split(',')] if len(sys.argv) > 2 else None)
