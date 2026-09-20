# -*- coding: utf-8 -*-
"""cudnn.benchmark × 变长 batch 的卡顿验收脚本

背景
----
素材训练会走人脸检测（avatars/*/face_detection/api.py:57、sfd/detect.py:25,63），
那里把 `torch.backends.cudnn.benchmark` 设成 True —— 这是**进程级全局开关，
设一次就不再恢复**。而训练与直播在同一个进程里，所以只要在服务里训练过一次
素材，之后所有直播推理都会跑在 benchmark=True 之下。
偏偏直播的批大小是变化的（wav2lip_avatar.inference_batch 按"当时有几个 mel
就绪"组批），warm_up 又只预热了 batch=16 这一个形状 —— 于是 cuDNN 会对
**每一个新形状**做一次算法基准测试，每个新形状耗时 1.9~5.8 秒，
线上表现就是「偶尔卡到 10 帧以下」。

修复见 commit 9908204：avatars/auto_loader.py 的 _disable_cudnn_benchmark()
在每次会话的 get() 最前面把开关复位。

用法
----
    python benchmark_cudnn.py                  # 验收（默认 wav2lip），期望 PASS
    python benchmark_cudnn.py --kind musetalk  # 验收 musetalk
    python benchmark_cudnn.py --raw 1          # 对照：复现旧行为（能看到秒级）
    python benchmark_cudnn.py --raw 0          # 对照：一直关着

判定（验收模式）
--------------
    ① auto_models.get() 之后 cudnn.benchmark 必须是 False（自愈生效）
    ② 每个新形状的首次调用不得超过 --threshold-ms（默认 1000ms）
两条都满足才打印 PASS。
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 必须按 app.py 的顺序先把顶层包钉进 sys.modules：否则 musetalk 的导入链会把
# `utils` 解析成别的东西（ModuleNotFoundError: 'utils' is not a package）
import utils.logger  # noqa: E402,F401
import utils.device  # noqa: E402,F401
import registry  # noqa: E402,F401
import torch  # noqa: E402


def make_run(kind, model, mod):
    """返回 run(B)：跑一次该批大小的真实推理（wav2lip）或预热（musetalk）。"""
    if kind == 'wav2lip':
        dev = mod.device

        def run(B):
            mel = torch.ones(B, 1, 80, 16, device=dev)
            img = torch.ones(B, 6, 256, 256, device=dev)
            with torch.no_grad():
                model(mel, img)

        return run

    def run(B):
        mod.warm_up(B, model)

    return run


def load_direct(kind):
    """绕过 auto_models（即绕过自愈），直接加载模型 —— 只用于 --raw 对照。"""
    if kind == 'wav2lip':
        import avatars.wav2lip_avatar as m
        return m.load_model('./models/wav2lip.pth'), m
    import avatars.musetalk_avatar as m
    return m.load_model(), m


def main():
    ap = argparse.ArgumentParser(description='cudnn.benchmark × 变长 batch 验收')
    ap.add_argument('--kind', choices=['wav2lip', 'musetalk'], default='wav2lip')
    ap.add_argument('--batches', default='1,2,3,4,5,6,8,12,16',
                    help='要测的批大小（逗号分隔），默认覆盖直播里会出现的范围')
    ap.add_argument('--reps', type=int, default=2, help='每个形状重复几次（第 1 次=新形状）')
    ap.add_argument('--raw', type=int, choices=[0, 1], default=None,
                    help='不给=验收模式；0/1=强制该值并绕过自愈（对照用）')
    ap.add_argument('--threshold-ms', type=float, default=1000.0,
                    help='新形状首次调用的上限，超过即 FAIL')
    a = ap.parse_args()

    batches = [int(x) for x in a.batches.split(',') if x.strip()]
    healed = None

    if a.raw is None:
        # ── 验收模式：先模拟"训练干过的坏事"，再走真实会话路径 ──
        torch.backends.cudnn.benchmark = True
        print('[验收] 已模拟训练污染：cudnn.benchmark = True')
        from avatars import auto_loader
        model, mod = auto_loader.auto_models.get(a.kind, batch_size=max(batches))
        healed = torch.backends.cudnn.benchmark is False
        print('[验收] auto_models.get() 之后 cudnn.benchmark = %s  -> %s'
              % (torch.backends.cudnn.benchmark, '已自愈 OK' if healed else '未复位 FAIL'))
    else:
        # ── 对照模式：不做任何干预，直接加载 ──
        torch.backends.cudnn.benchmark = bool(a.raw)
        model, mod = load_direct(a.kind)
        torch.backends.cudnn.benchmark = bool(a.raw)
        print('[对照] 强制 cudnn.benchmark = %s（未走自愈）' % torch.backends.cudnn.benchmark)

    run = make_run(a.kind, model, mod)

    # 真实会话里 warm_up 已经跑过 max(batches)，这里对齐一下再做记录
    run(max(batches))
    torch.cuda.empty_cache()

    print()
    print('%3s  %-10s %s' % ('B', '首次(ms)', '   '.join('rep%d(ms)' % (i + 2) for i in range(a.reps - 1))) + '   reserved')
    rows = []
    for B in batches:
        times = []
        for _ in range(a.reps):
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            run(B)
            torch.cuda.synchronize()
            times.append((time.perf_counter() - t0) * 1000.0)
        res = torch.cuda.memory_reserved() / 2**20
        rows.append({'B': B, 'first': times[0], 'times': times, 'res': res})
        rest = '   '.join('%9.1f' % t for t in times[1:])
        print('%3d  %9.1f  %s   %7.0fMB' % (B, times[0], rest, res), flush=True)

    firsts = [r['first'] for r in rows]
    worst = max(firsts)
    over = [r['B'] for r in rows if r['first'] > a.threshold_ms]

    print()
    print('[结果] kind=%s  新形状首次: 最差 %.1fms  合计 %.1fms  超阈值(%s)的形状=%s'
          % (a.kind, worst, sum(firsts), a.threshold_ms, over or '无'))

    ok = (worst <= a.threshold_ms) and (healed is not False)
    print('[判定] %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
