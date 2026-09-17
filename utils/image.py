
import os
import cv2
import numpy as np
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed


def imwrite_u(path, img, params=None):
    """UTF-8 路径安全的图像写入（替代 cv2.imwrite）。

    ⚠️ 为什么需要这个函数：
      Windows 上 OpenCV 的 cv2.imwrite() **无法写入含非 ASCII 字符的路径**
      （中文/日文/emoji 等）。它不抛异常，只**静默返回 False**，
      导致「任务显示成功但一个文件都没有」这种极难排查的问题。
      实测：
          cv2.imwrite("./data/avatars/_ascii/full_imgs/0.png", img) -> True
          cv2.imwrite("./data/avatars/数字人A/.../0.png", img)      -> False  ← 无任何报错

      本函数用 cv2.imencode 编码到内存，再用内置 open() 写文件，
      open() 走的是 Windows 宽字符 API，中文路径完全正常。
      （PIL 也可以，但 cv2.imencode 不引入额外依赖且与 OpenCV 编码参数一致。）

    返回 True/False，语义与 cv2.imwrite 一致。
    """
    try:
        ext = os.path.splitext(path)[1] or '.png'
        ok, buf = cv2.imencode(ext, img, params or [])
        if not ok:
            return False
        # 确保父目录存在（上游多处未建目录，cv2.imwrite 也是静默失败）
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(buf.tobytes())
        return True
    except Exception:
        return False


def imread_u(path, flags=cv2.IMREAD_COLOR):
    """UTF-8 路径安全的图像读取（替代 cv2.imread）。

    cv2.imread() 在 Windows 上同样无法读取含非 ASCII 的路径（静默返回 None）。
    用 np.fromfile + cv2.imdecode 绕过。
    """
    try:
        data = np.fromfile(path, dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None

# def read_imgs(img_list):
#     frames = []
#     logger.info('reading images...')
#     for img_path in tqdm(img_list):
#         frame = cv2.imread(img_path)
#         frames.append(frame)
#     return frames

def read_imgs(img_list):
    def load_image(index, img_path):
        # 用 imread_u 而非 cv2.imread：中文路径下 cv2.imread 静默返回 None，
        # 会让后续 face_list_cycle[0].shape 炸出难懂的 IndexError/AttributeError。
        img = imread_u(img_path)
        if img is None:
            img = cv2.imread(img_path)   # 兜底（非 ASCII 环境外无差别）
        return index, img

    frames = [None] * len(img_list)  # Initialize a list with the same length as img_list
    # max_workers=4：上游不设上限会让 ThreadPoolExecutor 按 CPU 核数开线程，
    # 而每张 PNG 解码都要一次内存分配 + 一次 numpy copy，线程过多反而抢 GIL/带宽。
    # 实测 4 路是最稳的（再高收益递减，且 1080p 长素材下瞬时内存占用明显变大）。
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(load_image, idx, img_path): idx for idx, img_path in enumerate(img_list)}
        for future in tqdm(as_completed(futures), total=len(img_list)):
            idx, img = future.result()
            frames[idx] = img
    return frames

def mirror_index(size, index):
    # 顺序循环：播到末尾后回到第 0 帧从头继续。
    #
    # 上游原实现是「乒乓绕回」——播完倒着播回来：
    #     turn = index // size; res = index % size
    #     return res if turn % 2 == 0 else size - res - 1
    # 该行为会让动作倒放（抬手 → 落手 → 抬手），对「多段头尾相连」的
    # 链式素材是灾难：素材一旦走到段尾，全部帧被倒序播放。
    # 若确实需要乒乓效果，请自行准备首尾相接的素材，不要改回此处。
    return index % size