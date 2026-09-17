from os import listdir, path
import numpy as np
import scipy, cv2, os, sys, argparse
import json, subprocess, random, string
from tqdm import tqdm
from glob import glob
import torch
import pickle
from avatars.wav2lip import face_detection
from utils.image import imwrite_u, imread_u


device = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Using {} for inference.'.format(device))

def osmakedirs(path_list):
    for path in path_list:
        os.makedirs(path) if not os.path.exists(path) else None

def video2imgs(vid_path, save_path, ext = '.png',cut_frame = 10000000):
    cap = cv2.VideoCapture(vid_path)
    count = 0
    while True:
        if count > cut_frame:
            break
        ret, frame = cap.read()
        if ret:
            # 原上游在此处把水印烧进素材帧（不可逆，播放时永远带着）：
            #     cv2.putText(frame, "LiveTalking", (10, 20),
            #                 cv2.FONT_HERSHEY_SIMPLEX, 0.3, (128,128,128), 1)
            # 已移除。注意：**已训练好的素材里水印是烧死的**，改这里只对"以后
            # 新训练的素材"生效；老素材要重训才会变干净。
            # 用 imwrite_u 而非 cv2.imwrite：后者在中文路径下静默失败返回 False，
            # 会导致"训练显示成功但没有任何帧"（素材名含中文时必现）。
            if not imwrite_u(f"{save_path}/{count:08d}.png", frame):
                print(f'[WARN] 写图失败: {save_path}/{count:08d}.png')
            count += 1
        else:
            break

def read_imgs(img_list):
    frames = []
    print('reading images...')
    for img_path in tqdm(img_list):
        # 用 imread_u：cv2.imread 在中文路径下静默返回 None，
        # 会让后面 face_detection 报 'NoneType' and 'int' 这种看不懂的错。
        frame = imread_u(img_path)
        if frame is None:
            print(f'[WARN] 读图失败(路径可能含非 ASCII): {img_path}')
        frames.append(frame)
    return frames

def get_smoothened_boxes(boxes, T):
	for i in range(len(boxes)):
		if i + T > len(boxes):
			window = boxes[len(boxes) - T:]
		else:
			window = boxes[i : i + T]
		boxes[i] = np.mean(window, axis=0)
	return boxes

def generate_avatar(video_path, avatar_id, save_path='./data/avatars', img_size=96, pads=[0, 10, 0, 0], nosmooth=False, face_det_batch_size=16, progress_callback=None):
    """
    生成avatar的核心逻辑

    Args:
        video_path: 输入视频路径
        avatar_id: Avatar ID
        save_path: 保存根路径
        img_size: 缩放后的图像大小
        pads: 人脸框填充 [top, bottom, left, right]
        nosmooth: 是否禁用平滑
        face_det_batch_size: 人脸检测批处理大小
        progress_callback: 进度回调函数，接收 0-100 的整数
    """
    avatar_path = os.path.join(save_path, avatar_id)
    full_imgs_path = os.path.join(avatar_path, "full_imgs")
    face_imgs_path = os.path.join(avatar_path, "face_imgs")
    coords_path = os.path.join(avatar_path, "coords.pkl")

    osmakedirs([avatar_path, full_imgs_path, face_imgs_path])

    if progress_callback: progress_callback(5)

    print(f"正在处理视频: {video_path}")
    video2imgs(video_path, full_imgs_path, ext='png')

    if progress_callback: progress_callback(20)

    input_img_list = sorted(glob(os.path.join(full_imgs_path, '*.[jpJP][pnPN]*[gG]')))
    frames = read_imgs(input_img_list)

    if progress_callback: progress_callback(40)

    print('正在检测人脸...')
    detector = face_detection.FaceAlignment(face_detection.LandmarksType._2D,
                                            flip_input=False, device=device)

    batch_size = 1
    _report_every = max(1, int(face_det_batch_size))
    predictions = []

    # ⚠️ 这里必须逐帧检测（batch_size=1），不能按 face_det_batch_size 真的批处理。
    #
    # 原因：SFDetector.get_detections_for_batch() 内部的 batch_detect() 是**伪批处理**
    #   —— 它对每个 anchor 位置做 Python 循环，循环次数只由特征图尺寸决定、与 batch 无关，
    #   但每轮迭代都构造带整个 batch 维度的 tensor 并单独调用一次 batch_decode()。
    #   于是 batch 越大，总开销线性增长（实测 1080p / RTX 4060 Ti）：
    #       batch=1 → 239 ms/帧      batch=2 → 2182 ms/帧（慢 9.1 倍）
    #       batch=4 → 2670 ms/帧     batch=8 → 3722 ms/帧（慢 15.6 倍）
    #   页面默认 face_det_batch_size=4 时，394 帧需 ~17 分钟且经常跑不完 →
    #   这就是「训练从来没有成功过」的根因。
    #
    # 另外它还有**正确性**问题：循环里 `for Iindex, hindex, windex in poss` 取了
    #   Iindex 却从未使用，把整个 batch 的检测结果混进同一个列表，
    #   而后续 NMS 隐含假设各图 anchor 位置一致 → 部分图未激活的 anchor 会把
    #   其他图的 score 计进来，导致框偏移。实测同一帧 batch=1 得 (356,477,840,1051)
    #   而 batch=6 得 (343,495,881,1031)，最大偏差 40px。
    #
    # 所以固定 batch_size = 1；face_det_batch_size 仅保留为「进度上报粒度」，
    # 让界面在长视频上仍有较平滑的百分比反馈（不再影响实际速度）。
    #
    # 注：因为恒为 1，原 OOM 降 batch 重试逻辑已不可能触发，故移除。
    for i in range(0, len(frames), batch_size):
        predictions.extend(detector.get_detections_for_batch(np.array(frames[i:i + batch_size])))
        if progress_callback and (i % _report_every == 0):
            progress = 40 + int((i + batch_size) / len(frames) * 40)
            progress_callback(min(progress, 80))

    results = []
    pady1, pady2, padx1, padx2 = pads
    for rect, image in zip(predictions, frames):
        if rect is None:
            rect = [0, 0, image.shape[1], image.shape[0]]

        y1 = max(0, rect[1] - pady1)
        y2 = min(image.shape[0], rect[3] + pady2)
        x1 = max(0, rect[0] - padx1)
        x2 = min(image.shape[1], rect[2] + padx2)
        results.append([x1, y1, x2, y2])

    boxes = np.array(results)
    if not nosmooth:
        boxes = get_smoothened_boxes(boxes, T=5)

    if progress_callback: progress_callback(85)

    coord_list = []
    print(f"正在保存人脸图片和坐标...")
    for idx, (rect, frame) in enumerate(zip(boxes, frames)):
        face_frame = frame[int(rect[1]):int(rect[3]), int(rect[0]):int(rect[2])]
        resized_crop_frame = cv2.resize(face_frame, (img_size, img_size))
        imwrite_u(f"{face_imgs_path}/{idx:08d}.png", resized_crop_frame)
        coord_list.append((int(rect[1]), int(rect[3]), int(rect[0]), int(rect[2])))

        if progress_callback:
            progress = 85 + int((idx + 1) / len(boxes) * 15)
            progress_callback(progress)

    print(f"写入数据到坐标文件: {coords_path}")
    with open(coords_path, 'wb') as f:
        pickle.dump(coord_list, f)

    del detector
    if progress_callback: progress_callback(100)
    print("Avatar 生成完成！")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Inference code to lip-sync videos in the wild using Wav2Lip models')
    parser.add_argument('--img_size', default=96, type=int)
    parser.add_argument('--avatar_id', default='wav2lip_avatar1', type=str)
    parser.add_argument('--save_path', default='data/avatars', type=str)
    parser.add_argument('--video_path', default='', type=str)
    parser.add_argument('--nosmooth', default=False, action='store_true',
                        help='Prevent smoothing face detections over a short temporal window')
    parser.add_argument('--pads', nargs='+', type=int, default=[0, 10, 0, 0],
                        help='Padding (top, bottom, left, right). Please adjust to include chin at least')
    parser.add_argument('--face_det_batch_size', type=int,
                        help='Batch size for face detection', default=16)
    args = parser.parse_args()

    generate_avatar(
        video_path=args.video_path,
        avatar_id=args.avatar_id,
        save_path=args.save_path,
        img_size=args.img_size,
        pads=args.pads,
        nosmooth=args.nosmooth,
        face_det_batch_size=args.face_det_batch_size
    )
