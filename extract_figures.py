#!/usr/bin/env python3
"""
从学术 PDF 中提取完整 figure，并把对应 caption 一起裁进同一张图片。

用法:
    python extract_figures.py paper_002.pdf
    python extract_figures.py ./pdfs -o ./figure_output
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("[错误] 缺少依赖，请先运行: pip install pymupdf")
    sys.exit(1)


# 用于匹配 "Figure 1." / "Fig. 2:" 这类 caption 开头
CAPTION_RE = re.compile(r"^(Figure|Fig\.)\s*(\d+[A-Za-z]?)\s*[:.]", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")
# 通过 caption 文本启发式识别“图表类” figure，默认跳过
CHART_PATTERNS = [
    re.compile(r"\borbital population\b", re.IGNORECASE),
    re.compile(r"\baverage and maximum\b", re.IGNORECASE),
    re.compile(r"\bvariation of\b", re.IGNORECASE),
    re.compile(r"\b(line|bar)\s+chart\b", re.IGNORECASE),
    re.compile(r"\bhistogram\b", re.IGNORECASE),
    re.compile(r"\bscatter plot\b", re.IGNORECASE),
    re.compile(r"\bplot of\b", re.IGNORECASE),
    re.compile(r"\bcurves?\b", re.IGNORECASE),
    re.compile(r"\bduring molecular dynamics simulation\b", re.IGNORECASE),
]


@dataclass
class CaptionBlock:
    """保存单个 caption 文本块及其位置信息。"""

    page_num: int
    figure_id: str
    text: str
    bbox: fitz.Rect
    column: str


def normalize_text(text: str) -> str:
    """清理多余空白，避免换行影响 caption 匹配。"""
    return SPACE_RE.sub(" ", (text or "").replace("\xa0", " ")).strip()


def safe_stem(path: Path) -> str:
    """把文件名转换成适合目录名的安全字符串。"""
    return re.sub(r"[^\w\-]+", "_", path.stem).strip("_") or "document"


def classify_column(rect: fitz.Rect, page_width: float) -> str:
    """根据 bbox 所在位置判断是左栏、右栏还是通栏。"""
    center = rect.x0 + rect.width / 2
    mid = page_width / 2
    if rect.width >= page_width * 0.7:
        return "full"
    # 某些通栏 caption 实际文本块没有铺满整页，但会明显居中
    if rect.width >= page_width * 0.58 and abs(center - mid) <= page_width * 0.08:
        return "full"
    return "left" if center < mid else "right"


def overlap_width(rect_a: fitz.Rect, rect_b: fitz.Rect) -> float:
    """计算两个矩形在水平方向上的重叠宽度。"""
    return max(0.0, min(rect_a.x1, rect_b.x1) - max(rect_a.x0, rect_b.x0))


def expand_rect(rect: fitz.Rect, page_rect: fitz.Rect, pad: float) -> fitz.Rect:
    """在页面范围内对矩形四周增加留白。"""
    return fitz.Rect(
        max(page_rect.x0, rect.x0 - pad),
        max(page_rect.y0, rect.y0 - pad),
        min(page_rect.x1, rect.x1 + pad),
        min(page_rect.y1, rect.y1 + pad),
    )


def clip_to_rect(rect: fitz.Rect, bounds: fitz.Rect) -> fitz.Rect:
    """把矩形裁切到指定边界内。"""
    return fitz.Rect(
        max(bounds.x0, rect.x0),
        max(bounds.y0, rect.y0),
        min(bounds.x1, rect.x1),
        min(bounds.y1, rect.y1),
    )


def union_rects(rects: list[fitz.Rect]) -> fitz.Rect | None:
    """把多个矩形合并成一个最小外接矩形。"""
    if not rects:
        return None
    result = fitz.Rect(rects[0])
    for rect in rects[1:]:
        result |= rect
    return result


def find_runs(mask: list[bool], min_len: int) -> list[tuple[int, int]]:
    """把布尔序列中的连续 True 区间提取出来。"""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for idx, value in enumerate(mask):
        if value and start is None:
            start = idx
        elif not value and start is not None:
            if idx - start >= min_len:
                runs.append((start, idx))
            start = None
    if start is not None and len(mask) - start >= min_len:
        runs.append((start, len(mask)))
    return runs


def merge_short_gaps(mask: list[bool], max_gap: int) -> list[bool]:
    """把很短的空白间隔合并掉，避免一个 figure 被切成多段。"""
    merged = mask[:]
    gap_start: int | None = None
    for idx, value in enumerate(merged):
        if not value and gap_start is None:
            gap_start = idx
        elif value and gap_start is not None:
            if idx - gap_start <= max_gap:
                for gap_idx in range(gap_start, idx):
                    merged[gap_idx] = True
            gap_start = None
    return merged


def find_captions(page: fitz.Page, page_num: int) -> list[CaptionBlock]:
    """从当前页文本块里找出所有 Figure/Fig. caption。"""
    captions: list[CaptionBlock] = []
    for block in page.get_text("blocks"):
        if len(block) < 5:
            continue
        text = normalize_text(block[4])
        match = CAPTION_RE.match(text)
        if not match:
            continue
        bbox = fitz.Rect(block[:4])
        captions.append(
            CaptionBlock(
                page_num=page_num,
                figure_id=match.group(2),
                text=text,
                bbox=bbox,
                column=classify_column(bbox, page.rect.width),
            )
        )
    captions.sort(key=lambda item: (item.bbox.y0, item.bbox.x0))
    return captions


def chart_like_reason(caption_text: str) -> str | None:
    """如果 caption 像柱状图/折线图描述，则返回命中的关键词。"""
    for pattern in CHART_PATTERNS:
        match = pattern.search(caption_text)
        if match:
            return match.group(0)
    return None


def page_image_rects(page: fitz.Page, min_width: float, min_height: float) -> list[fitz.Rect]:
    """提取页面内嵌位图区域，并过滤掉过小图片。"""
    rects: list[fitz.Rect] = []
    for info in page.get_image_info(hashes=True):
        bbox = info.get("bbox")
        if not bbox:
            continue
        rect = fitz.Rect(bbox)
        if rect.width < min_width or rect.height < min_height:
            continue
        rects.append(rect)
    return rects


def page_drawing_rects(page: fitz.Page) -> list[fitz.Rect]:
    """提取页面中的矢量绘图区域，用于纯矢量 figure 回退识别。"""
    rects: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if not rect:
            continue
        if rect.width <= 0 or rect.height <= 0:
            continue
        rects.append(fitz.Rect(rect))
    return rects


def column_rect(page_rect: fitz.Rect, column: str) -> fitz.Rect:
    """根据栏位类型返回当前栏的大致裁剪区域。"""
    mid = page_rect.x0 + page_rect.width / 2
    if column == "left":
        return fitz.Rect(page_rect.x0, page_rect.y0, mid + 8, page_rect.y1)
    if column == "right":
        return fitz.Rect(mid - 8, page_rect.y0, page_rect.x1, page_rect.y1)
    return fitz.Rect(page_rect)


def previous_caption_bottom(
    page_rect: fitz.Rect,
    caption: CaptionBlock,
    captions: list[CaptionBlock],
    floor_y: float,
    target_column: str | None = None,
) -> float:
    """找出当前导出区域上方，最近一条可作为分隔线的 caption 下边界。"""
    bottom = floor_y
    current_column = target_column or caption.column
    current_col_rect = column_rect(page_rect, current_column)
    for other in captions:
        if other is caption:
            continue
        if other.bbox.y1 <= caption.bbox.y0:
            other_col_rect = column_rect(page_rect, other.column)
            if overlap_width(current_col_rect, other_col_rect) > 0:
                bottom = max(bottom, other.bbox.y1)
    return bottom


def select_graphic_rects(
    rects: list[fitz.Rect],
    clip_column: fitz.Rect,
    top_y: float,
    bottom_y: float,
) -> list[fitz.Rect]:
    """筛出位于 caption 上方、且与当前栏足够重叠的图形区域。"""
    selected: list[fitz.Rect] = []
    for rect in rects:
        if rect.y1 > bottom_y or rect.y0 < top_y:
            continue
        if overlap_width(rect, clip_column) / max(rect.width, 1.0) < 0.6:
            continue
        selected.append(rect)
    return selected


def detect_raster_content_band(page: fitz.Page, clip_rect: fitz.Rect) -> fitz.Rect | None:
    """在候选区域内按像素寻找最大的连续视觉块，用于整页大图/扫描页回退分割。"""
    if clip_rect.width < 20 or clip_rect.height < 20:
        return None

    pix = page.get_pixmap(clip=clip_rect, dpi=96, alpha=False)
    width = pix.width
    height = pix.height
    channels = pix.n
    if width <= 0 or height <= 0:
        return None

    row_mask: list[bool] = []
    samples = memoryview(pix.samples)
    stride = width * channels

    for row_idx in range(height):
        row = samples[row_idx * stride:(row_idx + 1) * stride]
        dark_pixels = 0
        for offset in range(0, len(row), channels):
            avg = (row[offset] + row[offset + 1] + row[offset + 2]) // 3
            if avg < 245:
                dark_pixels += 1
        row_mask.append((dark_pixels / width) >= 0.01)

    row_mask = merge_short_gaps(row_mask, max_gap=max(2, height // 80))
    runs = find_runs(row_mask, min_len=max(10, height // 18))
    if not runs:
        return None

    # 优先选择最高、且更靠近当前 caption 的视觉块
    best_start, best_end = max(runs, key=lambda item: ((item[1] - item[0]), item[1]))
    scale_y = clip_rect.height / height
    y0 = clip_rect.y0 + best_start * scale_y
    y1 = clip_rect.y0 + best_end * scale_y
    return fitz.Rect(clip_rect.x0, y0, clip_rect.x1, y1)


def infer_effective_column(
    page_rect: fitz.Rect,
    caption: CaptionBlock,
    image_rects: list[fitz.Rect],
    drawing_rects: list[fitz.Rect],
    top_y: float,
    bottom_y: float,
) -> str:
    """结合 caption 和图形区域，判断当前 figure 实际应按单栏还是通栏处理。"""
    if caption.column == "full":
        return "full"

    mid = page_rect.x0 + page_rect.width / 2
    band_rects = []
    for rect in image_rects + drawing_rects:
        if rect.y1 > bottom_y or rect.y0 < top_y:
            continue
        band_rects.append(rect)

    for rect in band_rects:
        # 图形本体明显跨过中线，通常应视为通栏
        if rect.width >= page_rect.width * 0.72:
            return "full"
        if rect.x0 < mid - 24 and rect.x1 > mid + 24:
            return "full"

    return caption.column


def locate_figure_rect(
    page: fitz.Page,
    caption: CaptionBlock,
    captions: list[CaptionBlock],
    image_rects: list[fitz.Rect],
    drawing_rects: list[fitz.Rect],
    padding: float,
    top_floor: float,
) -> tuple[fitz.Rect | None, str, str]:
    """为某个 caption 定位对应的 figure 主体区域。"""
    page_rect = page.rect
    initial_top_y = previous_caption_bottom(
        page_rect,
        caption,
        captions,
        max(page_rect.y0, top_floor),
    )
    bottom_y = max(initial_top_y, caption.bbox.y0 - 6)
    effective_column = infer_effective_column(
        page_rect=page_rect,
        caption=caption,
        image_rects=image_rects,
        drawing_rects=drawing_rects,
        top_y=initial_top_y,
        bottom_y=bottom_y,
    )
    top_y = previous_caption_bottom(
        page_rect,
        caption,
        captions,
        max(page_rect.y0, top_floor),
        target_column=effective_column,
    )
    bottom_y = max(top_y, caption.bbox.y0 - 6)
    col_rect = column_rect(page_rect, effective_column)

    # 先优先使用位图区域定位 figure
    graphics = select_graphic_rects(image_rects, col_rect, top_y, bottom_y)
    source = "images"

    if not graphics:
        # 如果没有位图，再尝试用矢量绘图区域定位
        graphics = select_graphic_rects(drawing_rects, col_rect, top_y, bottom_y)
        source = "drawings"

    if graphics:
        rect = union_rects(graphics)
        if rect:
            rect = fitz.Rect(
                max(col_rect.x0, rect.x0),
                max(top_y, rect.y0),
                min(col_rect.x1, rect.x1),
                min(bottom_y, rect.y1),
            )
            return expand_rect(rect, page_rect, padding), source, effective_column

    # 如果 PDF 内部没有独立图片块，尝试从整段页面渲染里找“最大的视觉内容块”
    raster_rect = detect_raster_content_band(page, fitz.Rect(col_rect.x0, top_y, col_rect.x1, bottom_y))
    if raster_rect:
        return expand_rect(raster_rect, page_rect, padding), "raster", effective_column

    # 实在找不到明确图形区域时，回退为“caption 上方整块区域”
    fallback = fitz.Rect(col_rect.x0, top_y, col_rect.x1, bottom_y)
    if fallback.width < 20 or fallback.height < 20:
        return None, "none", effective_column
    return expand_rect(fallback, page_rect, padding), "fallback", effective_column


def combine_figure_with_caption(
    figure_rect: fitz.Rect,
    caption: CaptionBlock,
    page_rect: fitz.Rect,
    padding: float,
    effective_column: str,
) -> fitz.Rect:
    """把 figure 主体和下方 caption 合并成同一导出区域。"""
    col_rect = column_rect(page_rect, effective_column)
    caption_rect = expand_rect(caption.bbox, page_rect, padding)
    combined = fitz.Rect(
        min(figure_rect.x0, caption_rect.x0),
        min(figure_rect.y0, caption_rect.y0),
        max(figure_rect.x1, caption_rect.x1),
        max(figure_rect.y1, caption_rect.y1),
    )
    return clip_to_rect(combined, col_rect)


def write_markdown(out_file: Path, pdf_name: str, figures: list[dict]) -> None:
    """生成 Markdown 索引，便于后续浏览和校对。"""
    lines = [f"# Figures From {pdf_name}", ""]
    if not figures:
        lines.append("未找到带有 caption 的 figure。")
    else:
        for item in figures:
            lines.append(f"## Figure {item['figure_id']} (Page {item['page']})")
            lines.append("")
            lines.append(f"![Figure {item['figure_id']}]({item['image_relpath']})")
            lines.append("")
            lines.append(item["caption"])
            lines.append("")
    out_file.write_text("\n".join(lines), encoding="utf-8")


def figure_sort_key(item: dict) -> tuple[int, int, str]:
    """按页码、figure 编号排序输出结果。"""
    match = re.match(r"(\d+)", str(item["figure_id"]))
    number = int(match.group(1)) if match else 10**9
    return (int(item["page"]), number, str(item["figure_id"]))


def process_pdf(
    pdf_path: Path,
    out_root: Path,
    dpi: int,
    padding: float,
    min_width: float,
    min_height: float,
    top_floor: float,
    skip_chart_like: bool,
) -> list[dict]:
    """处理单个 PDF，输出图片、JSON 和 Markdown。"""
    pdf_slug = safe_stem(pdf_path)
    pdf_out = out_root / pdf_slug
    image_dir = pdf_out / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []

    with fitz.open(pdf_path) as doc:
        print(f"[PDF] 正在处理: {pdf_path.name}")
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            captions = find_captions(page, page_index + 1)
            if not captions:
                continue

            # 同时收集位图和矢量图形区域，后续按优先级匹配
            image_rects = page_image_rects(page, min_width, min_height)
            drawing_rects = page_drawing_rects(page)

            for index, caption in enumerate(captions, start=1):
                reason = chart_like_reason(caption.text) if skip_chart_like else None
                if reason:
                    print(
                        f"  [跳过] 第 {caption.page_num} 页 Figure {caption.figure_id} "
                        f"命中图表关键词: {reason}"
                    )
                    continue

                figure_rect, source, effective_column = locate_figure_rect(
                    page,
                    caption,
                    captions,
                    image_rects,
                    drawing_rects,
                    padding,
                    top_floor,
                )
                if not figure_rect:
                    print(f"  [警告] 第 {caption.page_num} 页 Figure {caption.figure_id}：未找到可裁剪区域")
                    continue

                # 最终导出时把 figure 和 caption 合成一张图
                export_rect = combine_figure_with_caption(
                    figure_rect=figure_rect,
                    caption=caption,
                    page_rect=page.rect,
                    padding=padding,
                    effective_column=effective_column,
                )

                image_name = f"page_{caption.page_num:03d}_figure_{caption.figure_id}_{index}.png"
                image_path = image_dir / image_name
                pix = page.get_pixmap(clip=export_rect, dpi=dpi, alpha=False)
                pix.save(str(image_path))

                result = {
                    "pdf": pdf_path.name,
                    "page": caption.page_num,
                    "figure_id": caption.figure_id,
                    "caption": caption.text,
                    "column": effective_column,
                    "source": source,
                    "bbox": [round(export_rect.x0, 2), round(export_rect.y0, 2), round(export_rect.x1, 2), round(export_rect.y1, 2)],
                    "figure_bbox": [round(figure_rect.x0, 2), round(figure_rect.y0, 2), round(figure_rect.x1, 2), round(figure_rect.y1, 2)],
                    "caption_bbox": [round(caption.bbox.x0, 2), round(caption.bbox.y0, 2), round(caption.bbox.x1, 2), round(caption.bbox.y1, 2)],
                    "image": str(image_path),
                    "image_relpath": f"images/{image_name}",
                }
                results.append(result)
                print(
                    f"  [完成] 第 {caption.page_num} 页 Figure {caption.figure_id} "
                    f"通过 {source} 提取 -> {image_name}"
                )

    results.sort(key=figure_sort_key)

    (pdf_out / "figures.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(pdf_out / "figures.md", pdf_path.name, results)
    return results


def collect_pdfs(input_path: Path) -> list[Path]:
    """收集单个 PDF 或目录下全部 PDF。"""
    if input_path.is_dir():
        return sorted(input_path.glob("**/*.pdf"))
    return [input_path]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="直接从 PDF 中提取 figure 图片，并把对应 caption 一起导出。"
    )
    parser.add_argument("input", help="PDF 文件，或包含 PDF 的目录")
    parser.add_argument(
        "-o",
        "--output",
        default="figure_output",
        help="输出目录（默认：./figure_output）",
    )
    parser.add_argument("--dpi", type=int, default=300, help="导出图片的 DPI")
    parser.add_argument("--padding", type=float, default=8, help="裁剪区域四周增加的留白像素")
    parser.add_argument("--min-width", type=float, default=50, help="保留的最小内嵌图片宽度")
    parser.add_argument("--min-height", type=float, default=50, help="保留的最小内嵌图片高度")
    parser.add_argument(
        "--top-floor",
        type=float,
        default=55,
        help="忽略页面顶部此 y 坐标以上的候选图形区域",
    )
    parser.add_argument(
        "--skip-chart-like",
        dest="skip_chart_like",
        action="store_true",
        default=True,
        help="根据 caption 关键词跳过柱状图、折线图等图表类 figure（默认开启）",
    )
    parser.add_argument(
        "--keep-chart-like",
        dest="skip_chart_like",
        action="store_false",
        help="保留图表类 figure，不做过滤",
    )
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[错误] 路径不存在: {input_path}")
        sys.exit(1)

    pdf_files = collect_pdfs(input_path)
    if not pdf_files:
        print(f"[警告] 在该路径下未找到 PDF 文件: {input_path}")
        return

    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"[输入] {input_path.resolve()}")
    print(f"[输出] {out_root.resolve()}")
    print(f"[PDF 数量] {len(pdf_files)}")
    print(f"[过滤图表类] {args.skip_chart_like}")

    total = 0
    for pdf_path in pdf_files:
        results = process_pdf(
            pdf_path=pdf_path,
            out_root=out_root,
            dpi=args.dpi,
            padding=args.padding,
            min_width=args.min_width,
            min_height=args.min_height,
            top_floor=args.top_floor,
            skip_chart_like=args.skip_chart_like,
        )
        print(f"  [完成] {pdf_path.name}: 共提取 {len(results)} 个 figure")
        total += len(results)

    print(f"[完成] 全部共提取 {total} 个 figure")


if __name__ == "__main__":
    main()
