#!/usr/bin/env python3
"""
小红书卡片渲染脚本 (Pillow版) — 无需浏览器，纯Python渲染
使用 Pillow 生成小红书风格的卡片图片，不需要 Playwright / Chromium

安装依赖: pip install Pillow
"""

import argparse
import os
import sys
import textwrap
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ============ 配置 ============
CARD_WIDTH = 1080
CARD_HEIGHT = 1440
COVER_WIDTH = 1080
COVER_HEIGHT = 1440
PADDING = 80
LINE_HEIGHT = 56
PARAGRAPH_SPACING = 30
TITLE_SIZE = 72
BODY_SIZE = 42
FONT_REGULAR = None  # 自动选择
FONT_BOLD = None

# ============ 主题色板 ============
THEMES = {
    "professional": {
        "bg": (255, 255, 255),
        "title": (30, 30, 30),
        "body": (60, 60, 60),
        "accent": (52, 119, 235),
        "card_bg": (248, 249, 250),
        "cover_bg": (52, 119, 235),
        "cover_title": (255, 255, 255),
        "cover_subtitle": (220, 230, 255),
    },
    "default": {
        "bg": (255, 255, 255),
        "title": (30, 30, 30),
        "body": (80, 80, 80),
        "accent": (223, 55, 85),
        "card_bg": (250, 250, 250),
        "cover_bg": (223, 55, 85),
        "cover_title": (255, 255, 255),
        "cover_subtitle": (255, 220, 225),
    },
    "minimal": {
        "bg": (245, 245, 240),
        "title": (40, 40, 40),
        "body": (80, 80, 75),
        "accent": (180, 140, 100),
        "card_bg": (255, 255, 250),
        "cover_bg": (200, 180, 160),
        "cover_title": (255, 255, 255),
        "cover_subtitle": (245, 240, 230),
    },
    "tech": {
        "bg": (15, 23, 42),
        "title": (230, 240, 255),
        "body": (180, 200, 230),
        "accent": (0, 200, 255),
        "card_bg": (22, 33, 62),
        "cover_bg": (0, 120, 200),
        "cover_title": (255, 255, 255),
        "cover_subtitle": (180, 220, 255),
    },
}


def find_font():
    """查找系统字体"""
    font_paths = [
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        # Windows / WSL
        "/mnt/c/Windows/Fonts/msyh.ttc",
        "/mnt/c/Windows/Fonts/simhei.ttf",
        "/mnt/c/Windows/Fonts/yahei.ttf",
    ]
    
    for path in font_paths:
        if os.path.exists(path):
            return path
    
    # Fallback to default
    return None


def wrap_text(text, font, max_width):
    """按宽度换行文本"""
    lines = []
    for paragraph in text.split('\n'):
        if not paragraph.strip():
            lines.append('')
            continue
        
        words = list(paragraph)  # 中文字符按字拆分
        current_line = ''
        
        for char in paragraph:
            test_line = current_line + char
            bbox = font.getbbox(test_line)
            if bbox and bbox[2] - bbox[0] <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = char
        
        if current_line:
            lines.append(current_line)
    
    return lines


def create_cover(title, theme_name="professional"):
    """创建封面图片"""
    theme = THEMES.get(theme_name, THEMES["professional"])
    font_path = find_font()
    
    img = Image.new('RGB', (COVER_WIDTH, COVER_HEIGHT), theme["cover_bg"])
    draw = ImageDraw.Draw(img)
    
    # 装饰性元素
    for i in range(5):
        x = random.randint(50, COVER_WIDTH - 100)
        y = random.randint(100, COVER_HEIGHT - 100)
        size = random.randint(30, 80)
        alpha = random.randint(20, 60)
        overlay = Image.new('RGBA', (size, size), (255, 255, 255, alpha))
        img.paste(overlay, (x, y), overlay)
    
    # 标题
    if font_path:
        try:
            title_font = ImageFont.truetype(font_path, TITLE_SIZE)
        except:
            title_font = ImageFont.load_default()
    else:
        title_font = ImageFont.load_default()
    
    # 标题换行
    max_title_width = COVER_WIDTH - 2 * PADDING
    title_lines = wrap_text(title, title_font, max_title_width)
    
    # 计算标题总高度
    total_title_h = len(title_lines) * (TITLE_SIZE + 10)
    start_y = (COVER_HEIGHT - total_title_h) // 2 - 80
    
    for line in title_lines:
        if line:
            bbox = title_font.getbbox(line)
            tw = bbox[2] - bbox[0] if bbox else 0
            x = (COVER_WIDTH - tw) // 2
            draw.text((x, start_y), line, fill=theme["cover_title"], font=title_font)
        start_y += TITLE_SIZE + 10
    
    # 底部装饰文字
    subtitle_font = ImageFont.load_default()
    subtitle = "小红书笔记"
    bbox = subtitle_font.getbbox(subtitle)
    sw = bbox[2] - bbox[0] if bbox else 0
    draw.text(((COVER_WIDTH - sw) // 2, COVER_HEIGHT - 120), subtitle, fill=theme["cover_subtitle"], font=subtitle_font)
    
    return img


def create_card(content, page_num=1, theme_name="professional"):
    """创建正文卡片"""
    theme = THEMES.get(theme_name, THEMES["professional"])
    font_path = find_font()
    
    img = Image.new('RGB', (CARD_WIDTH, CARD_HEIGHT), theme["bg"])
    draw = ImageDraw.Draw(img)
    
    # 内部卡片背景
    card_margin = 30
    draw.rounded_rectangle(
        [card_margin, card_margin, CARD_WIDTH - card_margin, CARD_HEIGHT - card_margin],
        radius=24,
        fill=theme["card_bg"]
    )
    
    if font_path:
        try:
            body_font = ImageFont.truetype(font_path, BODY_SIZE)
        except:
            body_font = ImageFont.load_default()
    else:
        body_font = ImageFont.load_default()
    
    text_area_width = CARD_WIDTH - 2 * PADDING - 2 * card_margin
    text_start_x = PADDING + card_margin
    text_start_y = PADDING + card_margin + 40
    
    # 处理标题（第一行加粗效果）
    lines = content.strip().split('\n')
    final_lines = []
    for line in lines:
        sub_lines = wrap_text(line, body_font, text_area_width)
        final_lines.extend(sub_lines)
    
    # 绘制文本
    y = text_start_y
    max_y = CARD_HEIGHT - 120
    
    for i, line in enumerate(final_lines):
        if y > max_y:
            draw.text((text_start_x, y), "...", fill=theme["body"], font=body_font)
            break
        
        # Emoji 做特殊处理（通常占据更多空间，但这里简化为同一字体）
        if line.startswith('#') or i == 0:  # 标题行
            draw.text((text_start_x, y), line.lstrip('#').strip(), fill=theme["title"], font=body_font)
        elif line == '':
            y += PARAGRAPH_SPACING
            continue
        else:
            draw.text((text_start_x, y), line, fill=theme["body"], font=body_font)
        
        y += LINE_HEIGHT
    
    # 页码
    page_font = ImageFont.load_default()
    page_text = f"— {page_num} —"
    bbox = page_font.getbbox(page_text)
    pw = bbox[2] - bbox[0] if bbox else 0
    draw.text(((CARD_WIDTH - pw) // 2, CARD_HEIGHT - 60), page_text, fill=theme["accent"], font=page_font)
    
    return img


def parse_markdown(md_text):
    """解析Markdown内容，按---分隔符分割页面"""
    # 移除YAML frontmatter
    if md_text.startswith('---'):
        parts = md_text.split('---', 2)
        if len(parts) >= 3:
            md_text = parts[2]
    
    # 移除代码块 (如果有)
    lines = md_text.split('\n')
    in_code = False
    clean_lines = []
    for line in lines:
        if line.strip().startswith('```'):
            in_code = not in_code
            continue
        if not in_code:
            clean_lines.append(line)
    
    md_text = '\n'.join(clean_lines)
    
    # 按分隔符分割页面
    pages = []
    current_page = []
    
    for line in md_text.split('\n'):
        if line.strip() == '---':
            pages.append('\n'.join(current_page).strip())
            current_page = []
        else:
            current_page.append(line)
    
    if current_page:
        pages.append('\n'.join(current_page).strip())
    
    # 过滤太短或空的页面
    pages = [p for p in pages if len(p.strip()) > 10]
    
    return pages if pages else [md_text.strip()]


def main():
    parser = argparse.ArgumentParser(description='渲染小红书卡片 (Pillow版)')
    parser.add_argument('input', help='输入Markdown文件')
    parser.add_argument('-o', '--output-dir', default='./output', help='输出目录')
    parser.add_argument('-t', '--theme', default='professional',
                        choices=list(THEMES.keys()), help='主题')
    parser.add_argument('--width', type=int, default=1080, help='图片宽度')
    parser.add_argument('--height', type=int, default=1440, help='图片高度')
    
    args = parser.parse_args()
    
    # 读取Markdown
    with open(args.input, 'r', encoding='utf-8') as f:
        md_text = f.read()
    
    # 解析
    pages = parse_markdown(md_text)
    
    if not pages:
        print("❌ 没有可渲染的内容")
        sys.exit(1)
    
    # 准备输出目录
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 获取标题（第一页第一行）
    title = pages[0].split('\n')[0].strip().lstrip('#').strip()
    if not title:
        title = "小红书笔记"
    
    global CARD_WIDTH, CARD_HEIGHT
    CARD_WIDTH = args.width
    CARD_HEIGHT = args.height
    
    # 渲染封面
    print(f"🎨 渲染封面...")
    cover = create_cover(title, args.theme)
    cover_path = output_dir / 'cover.png'
    cover.save(cover_path)
    print(f"   ✅ 封面: {cover_path}")
    
    # 渲染正文卡片
    print(f"🎨 渲染正文 ({len(pages)} 页)...")
    for i, page_content in enumerate(pages):
        card = create_card(page_content, i + 1, args.theme)
        card_path = output_dir / f'card_{i + 1}.png'
        card.save(card_path)
        print(f"   ✅ 第{i+1}页: {card_path} ({len(page_content)}字)")
    
    print(f"\n✅ 完成! 共生成 {1 + len(pages)} 张图片")
    print(f"   输出目录: {output_dir.absolute()}")


if __name__ == '__main__':
    main()
