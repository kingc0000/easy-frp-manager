#!/usr/bin/env python3
"""
frpm 图标生成器(纯 Python 标准库,零依赖)。

生成 512x512 PNG 图标用于 Docker Hub。
"""
import os
import struct
import zlib


def create_png(width: int, height: int, pixel_func) -> bytes:
    """
    生成 PNG 文件。
    
    pixel_func(x, y) -> (r, g, b) 返回像素颜色。
    """
    # PNG 签名
    sig = b"\x89PNG\r\n\x1a\n"
    
    # IHDR chunk
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    
    # IDAT chunk
    raw_data = b""
    for y in range(height):
        raw_data += b"\x00"  # filter type: none
        for x in range(width):
            r, g, b = pixel_func(x, y)
            raw_data += bytes([r, g, b])
    
    compressed = zlib.compress(raw_data, 9)
    idat_crc = zlib.crc32(b"IDAT" + compressed)
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)
    
    # IEND chunk
    iend_crc = zlib.crc32(b"IEND")
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    
    return sig + ihdr + idat + iend


def frpm_icon_pixel(x: int, y: int) -> tuple:
    """
    生成 frpm 图标像素(512x512)。
    
    设计:深蓝渐变背景 + 圆角方形 + 中央 frp 网络符号
    """
    cx, cy = x + 0.5, y + 0.5
    
    # 背景渐变(深蓝到更深)
    t = (x + y) / 512
    bg_r = int(15 + t * 10)
    bg_g = int(20 + t * 20)
    bg_b = int(45 + t * 40)
    
    # 右上角光晕(淡蓝色)
    dist_to_corner = ((x - 420) ** 2 + (y - 90) ** 2) ** 0.5
    if dist_to_corner < 180:
        glow_alpha = (1 - dist_to_corner / 180) * 0.15
        bg_r = int(bg_r + glow_alpha * 60)
        bg_g = int(bg_g + glow_alpha * 110)
        bg_b = int(bg_b + glow_alpha * 180)
    
    r, g, b = bg_r, bg_g, bg_b
    
    # 中央 frp 网络符号
    # 左侧蓝色节点 (frpc)
    dist_left = ((x - 136) ** 2 + (y - 220) ** 2) ** 0.5
    if dist_left < 48:
        # 蓝色渐变
        t2 = dist_left / 48
        r = int(59 + t2 * 15)
        g = int(130 + t2 * 30)
        b = int(246 + t2 * 20)
    
    # 右侧绿色节点 (frps)
    dist_right = ((x - 376) ** 2 + (y - 220) ** 2) ** 0.5
    if dist_right < 48:
        t2 = dist_right / 48
        r = int(16 + t2 * 30)
        g = int(185 + t2 * 30)
        b = int(129 + t2 * 30)
    
    # 中间连线(虚线)
    if abs(y - 220) < 4 and 184 < x < 328:
        # 虚线效果:每 16 像素一段
        if (x // 16) % 2 == 0:
            r, g, b = 229, 231, 235  # 浅灰
    
    # 中间箭头(指向右侧)
    if 296 < x < 326 and 202 < y < 238:
        # 三角形:顶点在 (326, 220),底边在 x=296
        if x - 296 > abs(y - 220) * 3:
            r, g, b = 229, 231, 235
    
    # 文字 "frpc" (左侧节点中心)
    if 108 < x < 164 and 208 < y < 232:
        # 简化文字渲染:白色
        r, g, b = 255, 255, 255
    
    # 文字 "frps" (右侧节点中心)
    if 348 < x < 404 and 208 < y < 232:
        r, g, b = 255, 255, 255
    
    # 底部文字 "FRP" (大字)
    if 196 < x < 316 and 396 < y < 428:
        r, g, b = 255, 255, 255
    
    # 文字 "MANAGER" (小字)
    if 196 < x < 316 and 436 < y < 452:
        r, g, b = 147, 197, 253  # 淡蓝
    
    # 版本号 "v1.1.0"
    if 216 < x < 296 and 480 < y < 492:
        r, g, b = 107, 114, 128  # 灰色
    
    return (r, g, b)


def generate_icon():
    """生成 512x512 图标并保存。"""
    out_dir = "/root/frp-manager/docs"
    os.makedirs(out_dir, exist_ok=True)
    
    # 生成 PNG
    png_bytes = create_png(512, 512, frpm_icon_pixel)
    png_path = os.path.join(out_dir, "icon-512.png")
    with open(png_path, "wb") as f:
        f.write(png_bytes)
    print(f"✓ PNG 已生成: {png_path} ({len(png_bytes)} 字节)")
    
    # 同时生成小尺寸版本(64x64,用于 favicon)
    def resize_pixel(x, y):
        return frpm_icon_pixel(x * 8, y * 8)
    favicon_bytes = create_png(64, 64, resize_pixel)
    favicon_path = os.path.join(out_dir, "favicon.png")
    with open(favicon_path, "wb") as f:
        f.write(favicon_bytes)
    print(f"✓ Favicon 已生成: {favicon_path} ({len(favicon_bytes)} 字节)")
    
    return png_path, favicon_path


if __name__ == "__main__":
    generate_icon()
