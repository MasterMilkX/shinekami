#!/usr/bin/env python3

from pathlib import Path
from PIL import Image
import argparse
import shutil
from collections import deque, defaultdict

def center_crop_to(image: Image.Image, target_width: int, target_height: int) -> Image.Image:
    """
    Center-crop the image to the given dimensions.

    Example:
        --crop-to 128 128

    crops the frame from the center to 128x128 pixels.
    """
    w, h = image.size

    if target_width > w or target_height > h:
        raise ValueError(
            f"Crop-to size {target_width}x{target_height} is larger than "
            f"frame size {w}x{h}."
        )

    left = (w - target_width) // 2
    top = (h - target_height) // 2
    right = left + target_width
    bottom = top + target_height

    return image.crop((left, top, right, bottom))

def make_white_transparent(image: Image.Image, threshold: int = 245) -> Image.Image:
    """
    Convert only the outside/background white or near-white pixels to transparent.

    This uses a flood-fill from the edges of the image, so only white pixels
    connected to the outer border are removed. White pixels inside the character
    are preserved.
    """
    image = image.convert("RGBA")
    width, height = image.size
    pixels = image.load()

    def is_background_white(x: int, y: int) -> bool:
        r, g, b, a = pixels[x, y]
        return a > 0 and r >= threshold and g >= threshold and b >= threshold

    visited = set()
    queue = deque()

    # Start flood fill from all white pixels touching the image edges.
    for x in range(width):
        if is_background_white(x, 0):
            queue.append((x, 0))
            visited.add((x, 0))

        if is_background_white(x, height - 1):
            queue.append((x, height - 1))
            visited.add((x, height - 1))

    for y in range(height):
        if is_background_white(0, y):
            queue.append((0, y))
            visited.add((0, y))

        if is_background_white(width - 1, y):
            queue.append((width - 1, y))
            visited.add((width - 1, y))

    # Flood fill through connected background-white pixels.
    while queue:
        x, y = queue.popleft()

        r, g, b, a = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)

        neighbors = (
            (x + 1, y),
            (x - 1, y),
            (x, y + 1),
            (x, y - 1),
        )

        for nx, ny in neighbors:
            if 0 <= nx < width and 0 <= ny < height:
                if (nx, ny) not in visited and is_background_white(nx, ny):
                    visited.add((nx, ny))
                    queue.append((nx, ny))

    return image

def crop_frame(image: Image.Image, top: int = 0, right: int = 0, bottom: int = 0, left: int = 0) -> Image.Image:
    """
    Crop pixels from the frame using top, right, bottom, left offsets.

    Example:
        (16, 24, 32, 8)
    means:
        remove 16 px from top
        remove 24 px from right
        remove 32 px from bottom
        remove 8 px from left
    """
    w, h = image.size

    new_left = left
    new_top = top
    new_right = w - right
    new_bottom = h - bottom

    if new_left >= new_right or new_top >= new_bottom:
        raise ValueError(
            f"Crop offsets are too large for frame size {w}x{h}: "
            f"top={top}, right={right}, bottom={bottom}, left={left}"
        )

    return image.crop((new_left, new_top, new_right, new_bottom))

def get_alpha_bbox(image: Image.Image, alpha_threshold: int = 8):
    """
    Find the bounding box of visible/non-transparent pixels.

    Returns:
        (left, top, right, bottom) or None if the image is fully transparent.
    """
    image = image.convert("RGBA")
    alpha = image.getchannel("A")

    # Ignore barely-visible anti-aliasing noise.
    mask = alpha.point(lambda a: 255 if a >= alpha_threshold else 0)

    return mask.getbbox()


def trim_to_visible_content(
    image: Image.Image,
    padding: int = 0,
    alpha_threshold: int = 8,
) -> Image.Image | None:
    """
    Trim an image down to the bounding box of its visible pixels.

    Returns None if no visible pixels are found.
    """
    image = image.convert("RGBA")
    bbox = get_alpha_bbox(image, alpha_threshold=alpha_threshold)

    if bbox is None:
        return None

    left, top, right, bottom = bbox

    if padding > 0:
        w, h = image.size
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(w, right + padding)
        bottom = min(h, bottom + padding)

    return image.crop((left, top, right, bottom))


def place_on_canvas(
    sprite: Image.Image | None,
    canvas_width: int = 128,
    canvas_height: int = 128,
    anchor: str = "center",
) -> Image.Image:
    """
    Place a trimmed sprite onto a fixed-size transparent canvas.

    anchor options:
        center        - centers the sprite both horizontally and vertically
        bottom-center - centers horizontally and places feet near the bottom
    """
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))

    if sprite is None:
        return canvas

    sprite = sprite.convert("RGBA")
    sw, sh = sprite.size

    if sw > canvas_width or sh > canvas_height:
        raise ValueError(
            f"Trimmed sprite size {sw}x{sh} is larger than canvas "
            f"{canvas_width}x{canvas_height}. Use a larger --canvas-size "
            f"or crop/resize the source first."
        )

    if anchor == "center":
        paste_x = (canvas_width - sw) // 2
        paste_y = (canvas_height - sh) // 2

    elif anchor == "bottom-center":
        paste_x = (canvas_width - sw) // 2
        paste_y = canvas_height - sh

    else:
        raise ValueError(
            f"Unknown anchor: {anchor}. Use 'center' or 'bottom-center'."
        )

    canvas.paste(sprite, (paste_x, paste_y), sprite)
    return canvas

def get_visible_mask(image: Image.Image, alpha_threshold: int = 8) -> Image.Image:
    """
    Create a black/white mask from the image alpha channel.
    White pixels are visible sprite pixels.
    Black pixels are transparent background.
    """
    image = image.convert("RGBA")
    alpha = image.getchannel("A")
    return alpha.point(lambda a: 255 if a >= alpha_threshold else 0)


def dilate_mask(mask: Image.Image, radius: int = 0) -> Image.Image:
    """
    Expand visible areas in the mask so nearby sprite pieces merge together.

    This helps keep motion marks, music notes, cyberdeck pieces, ears, etc.
    attached to the same detected sprite.
    """
    if radius <= 0:
        return mask

    from PIL import ImageFilter

    # MaxFilter size must be odd.
    size = radius * 2 + 1
    return mask.filter(ImageFilter.MaxFilter(size))


def find_connected_components(mask: Image.Image, min_area: int = 20):
    """
    Find connected white-pixel components in a mask.

    Returns a list of component bounding boxes:
        [(left, top, right, bottom), ...]
    """
    mask = mask.convert("L")
    width, height = mask.size
    pixels = mask.load()

    visited = set()
    components = []

    def is_visible(x, y):
        return pixels[x, y] > 0

    for y in range(height):
        for x in range(width):
            if (x, y) in visited or not is_visible(x, y):
                continue

            queue = deque([(x, y)])
            visited.add((x, y))

            min_x = max_x = x
            min_y = max_y = y
            area = 0

            while queue:
                cx, cy = queue.popleft()
                area += 1

                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)

                neighbors = (
                    (cx + 1, cy),
                    (cx - 1, cy),
                    (cx, cy + 1),
                    (cx, cy - 1),
                    (cx + 1, cy + 1),
                    (cx - 1, cy - 1),
                    (cx + 1, cy - 1),
                    (cx - 1, cy + 1),
                )

                for nx, ny in neighbors:
                    if 0 <= nx < width and 0 <= ny < height:
                        if (nx, ny) not in visited and is_visible(nx, ny):
                            visited.add((nx, ny))
                            queue.append((nx, ny))

            if area >= min_area:
                # right/bottom are exclusive, like PIL crop boxes
                components.append((min_x, min_y, max_x + 1, max_y + 1))

    return components


def union_boxes(boxes):
    """
    Combine multiple bounding boxes into one bounding box.
    """
    if not boxes:
        return None

    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[2] for box in boxes)
    bottom = max(box[3] for box in boxes)

    return (left, top, right, bottom)


def expand_box(box, padding: int, image_width: int, image_height: int):
    """
    Add padding around a bounding box, clamped to image bounds.
    """
    left, top, right, bottom = box

    return (
        max(0, left - padding),
        max(0, top - padding),
        min(image_width, right + padding),
        min(image_height, bottom + padding),
    )


def place_sprite_on_canvas(
    sprite: Image.Image,
    canvas_width: int = 128,
    canvas_height: int = 128,
    anchor: str = "bottom-center",
) -> Image.Image:
    """
    Place a cropped sprite onto a fixed transparent canvas.

    anchor:
        center        = centered both ways
        bottom-center = centered horizontally, feet near bottom
    """
    sprite = sprite.convert("RGBA")
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))

    sw, sh = sprite.size

    if sw > canvas_width or sh > canvas_height:
        # resize the sprite to fit the canvas while preserving aspect ratio
        aspect_ratio = sw / sh
        if aspect_ratio > canvas_width / canvas_height:
            new_sw = canvas_width
            new_sh = int(canvas_width / aspect_ratio)
        else:
            new_sh = canvas_height
            new_sw = int(canvas_height * aspect_ratio)

        sprite = sprite.resize((new_sw, new_sh), resample=Image.LANCZOS)
        sw, sh = sprite.size
        # raise ValueError(
        #     f"Detected sprite is {sw}x{sh}, larger than canvas "
        #     f"{canvas_width}x{canvas_height}. Try a larger --canvas-size, "
        #     f"smaller --detect-padding, or less --merge-radius."
        # )

    if anchor == "center":
        paste_x = (canvas_width - sw) // 2
        paste_y = (canvas_height - sh) // 2
    elif anchor == "bottom-center":
        paste_x = (canvas_width - sw) // 2
        paste_y = canvas_height - sh
    else:
        raise ValueError(f"Unknown anchor: {anchor}")

    canvas.paste(sprite, (paste_x, paste_y), sprite)
    return canvas


def auto_extract_sprites_by_layout(
    image: Image.Image,
    cols: int,
    rows: int,
    total_frames: int,
    canvas_size: tuple[int, int] = (128, 128),
    alpha_threshold: int = 8,
    min_component_area: int = 20,
    merge_radius: int = 4,
    detect_padding: int = 2,
    anchor: str = "bottom-center",
):
    """
    Detect sprites from the whole sheet instead of slicing fixed cells.

    Uses the known cols/rows layout only to assign detected components to
    shime1, shime2, etc. This allows sprites to overlap outside their nominal
    grid cells as long as neighboring sprites do not physically touch.
    """
    image = image.convert("RGBA")
    width, height = image.size

    cell_width = width / cols
    cell_height = height / rows

    mask = get_visible_mask(image, alpha_threshold=alpha_threshold)
    merged_mask = dilate_mask(mask, radius=merge_radius)

    components = find_connected_components(
        merged_mask,
        min_area=min_component_area,
    )

    print(f"Detected {len(components)} visible components before assignment.")

    assigned = defaultdict(list)

    for box in components:
        left, top, right, bottom = box
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2

        col = int(center_x // cell_width)
        row = int(center_y // cell_height)

        col = max(0, min(cols - 1, col))
        row = max(0, min(rows - 1, row))

        frame_number = row * cols + col + 1

        if frame_number <= total_frames:
            assigned[frame_number].append(box)

    frames = []

    for frame_number in range(1, total_frames + 1):
        boxes = assigned.get(frame_number, [])

        if not boxes:
            print(f"Warning: no sprite detected for shime{frame_number}.png")
            blank = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
            frames.append(blank)
            continue

        combined_box = union_boxes(boxes)
        combined_box = expand_box(
            combined_box,
            padding=detect_padding,
            image_width=width,
            image_height=height,
        )

        sprite = image.crop(combined_box)

        frame = place_sprite_on_canvas(
            sprite,
            canvas_width=canvas_size[0],
            canvas_height=canvas_size[1],
            anchor=anchor,
        )

        print(
            f"shime{frame_number}.png: detected box={combined_box}, "
            f"sprite size={sprite.size[0]}x{sprite.size[1]}"
        )

        frames.append(frame)

    return frames

def split_shimeji_sheet(
    character_name: str,
    cols: int = 11,
    rows: int = 5,
    total_frames: int = 46,
    clear_old: bool = True,
    crop_offsets: tuple[int, int, int, int] = (0, 0, 0, 0),
    crop_to: tuple[int, int] | None = None,
    transparent_bg: bool = True,
    white_threshold: int = 245,
    auto_detect: bool = False,
    canvas_size: tuple[int, int] = (128, 128),
    detect_alpha_threshold: int = 8,
    min_component_area: int = 20,
    merge_radius: int = 4,
    detect_padding: int = 2,
    detect_anchor: str = "bottom-center",
):
    """
    Splits a Shimeji sprite sheet into shime1.png through shime46.png.

    Expected input:
        {character_name}-img.png

    Expected output folder:
        {character_name}-shimeji/img/Shimeji
    """

    # make a new folder for the shimejis using the template if doesn't already exist
    char_folder = f'{character_name}-shimeji'
    if not Path(char_folder).is_dir():
        shutil.copytree('SHIMEJI-TEMPLATE', char_folder)


    base_image_path = Path(f"{character_name}-img.png")
    bip2 = Path(f"{character_name}-spritesheet.png")
    output_dir = Path(f"{character_name}-shimeji/img/Shimeji")

    if not base_image_path.exists():
        # try alternate
        if bip2.exists():
            base_image_path = bip2
            # if using spritesheet, change the columns + rows to 6 x 8 and don't use transparent
            cols = 6
            rows = 8
            transparent_bg = False
        else:
            raise FileNotFoundError(f"Could not find input image: {base_image_path} or {bip2}")

    output_dir.mkdir(parents=True, exist_ok=True)

    if clear_old:
        for old_file in output_dir.glob("shime*.png"):
            old_file.unlink()

    image = Image.open(base_image_path).convert("RGBA")

    if transparent_bg:
        image = make_white_transparent(image, threshold=white_threshold)

    width, height = image.size
    cell_width = width // cols
    cell_height = height // rows

    if cell_width <= 0 or cell_height <= 0:
        raise ValueError("Grid size is too large for the image dimensions.")

    print(f"Input image: {base_image_path}")
    print(f"Image size: {width}x{height}")
    print(f"Grid: {cols} columns x {rows} rows")
    print(f"Crop offsets (top, right, bottom, left): {crop_offsets}")
    print(f"Each frame: {cell_width}x{cell_height}")
    print(f"Crop offsets (top, right, bottom, left): {crop_offsets}")
    print(f"Crop to: {crop_to}")
    print(f"Output folder: {output_dir}")
    frame_number = 1

    if auto_detect:
        frames = auto_extract_sprites_by_layout(
            image=image,
            cols=cols,
            rows=rows,
            total_frames=total_frames,
            canvas_size=canvas_size,
            alpha_threshold=detect_alpha_threshold,
            min_component_area=min_component_area,
            merge_radius=merge_radius,
            detect_padding=detect_padding,
            anchor=detect_anchor,
        )

        for i, frame in enumerate(frames, start=1):
            output_path = output_dir / f"shime{i}.png"
            frame.save(output_path)
            print(f"Saved {output_path}")

        frame_number = len(frames) + 1

    else:
        frame_number = 1

        for row in range(rows):
            for col in range(cols):
                if frame_number > total_frames:
                    break

                left = col * cell_width
                top = row * cell_height
                right = left + cell_width
                bottom = top + cell_height

                frame = image.crop((left, top, right, bottom))

                top_crop, right_crop, bottom_crop, left_crop = crop_offsets
                if top_crop or right_crop or bottom_crop or left_crop:
                    frame = crop_frame(
                        frame,
                        top=top_crop,
                        right=right_crop,
                        bottom=bottom_crop,
                        left=left_crop,
                    )

                if crop_to is not None:
                    target_width, target_height = crop_to
                    frame = center_crop_to(
                        frame,
                        target_width=target_width,
                        target_height=target_height,
                    )

                output_path = output_dir / f"shime{frame_number}.png"
                frame.save(output_path)

                print(f"Saved {output_path}")
                frame_number += 1

            if frame_number > total_frames:
                break

    print(f"Done! Created {frame_number - 1} frames.")

    # send to zip file for easy import
    shutil.make_archive(f"{character_name}", 'zip', char_folder)

    print(f"Exported to {character_name}.zip!")



def main():
    parser = argparse.ArgumentParser(
        description="Split a Shimeji template image into shime1.png through shime46.png."
    )

    parser.add_argument(
        "character_name",
        help='Character name, e.g. "johnny" for johnny-img.png',
    )

    parser.add_argument(
        "--cols",
        type=int,
        default=11,
        help="Number of columns in the sprite sheet. Default: 12",
    )

    parser.add_argument(
        "--rows",
        type=int,
        default=5,
        help="Number of rows in the sprite sheet. Default: 5",
    )

    parser.add_argument(
        "--frames",
        type=int,
        default=46,
        help="Total number of frames to export. Default: 46",
    )

    parser.add_argument(
        "--keep-old",
        action="store_true",
        help="Do not delete old shime*.png files before exporting.",
    )

    parser.add_argument(
        "--offset",
        type=int,
        nargs=4,
        metavar=("TOP", "RIGHT", "BOTTOM", "LEFT"),
        default=(0, 0, 0, 0),
        help="Crop offsets in pixels as: TOP RIGHT BOTTOM LEFT. Default: 0 0 0 0",
    )

    parser.add_argument(
        "--no-transparent-bg",
        action="store_true",
        help="Do not turn white background transparent.",
    )

    parser.add_argument(
        "--white-threshold",
        type=int,
        default=245,
        help="Threshold for treating near-white as background. Default: 245",
    )

    parser.add_argument(
        "--crop-to",
        type=int,
        nargs=2,
        metavar=("WIDTH", "HEIGHT"),
        default=None,
        help="Center-crop each frame to WIDTH HEIGHT pixels. Example: --crop-to 128 128",
    )

    parser.add_argument(
        "--bbox-canvas",
        type=int,
        nargs=2,
        metavar=("WIDTH", "HEIGHT"),
        default=None,
        help=(
            "Trim each sprite to its visible bounding box, then place it on a "
            "transparent WIDTH HEIGHT canvas. Example: --bbox-canvas 128 128"
        ),
    )

    parser.add_argument(
        "--bbox-padding",
        type=int,
        default=0,
        help="Extra transparent padding to keep around the detected sprite bounding box. Default: 0",
    )

    parser.add_argument(
        "--bbox-alpha-threshold",
        type=int,
        default=8,
        help="Alpha threshold for detecting visible sprite pixels. Default: 8",
    )

    parser.add_argument(
        "--bbox-anchor",
        choices=("center", "bottom-center"),
        default="center",
        help="How to place the trimmed sprite on the canvas. Default: center",
    )

    parser.add_argument(
        "--auto-detect",
        action="store_true",
        help=(
            "Automatically detect sprite bounding boxes from the full sheet "
            "instead of slicing exact grid cells."
        ),
    )

    parser.add_argument(
        "--canvas-size",
        type=int,
        nargs=2,
        metavar=("WIDTH", "HEIGHT"),
        default=(128, 128),
        help="Output frame canvas size. Default: 128 128",
    )

    parser.add_argument(
        "--detect-alpha-threshold",
        type=int,
        default=8,
        help="Alpha threshold for detecting visible sprite pixels. Default: 8",
    )

    parser.add_argument(
        "--min-component-area",
        type=int,
        default=20,
        help="Ignore detected components smaller than this many pixels. Default: 20",
    )

    parser.add_argument(
        "--merge-radius",
        type=int,
        default=4,
        help=(
            "How strongly to merge nearby sprite pieces before detection. "
            "Higher values attach nearby effects/props, but can accidentally "
            "merge neighboring sprites. Default: 4"
        ),
    )

    parser.add_argument(
        "--detect-padding",
        type=int,
        default=2,
        help="Padding around detected sprite boxes before placing on canvas. Default: 2",
    )

    parser.add_argument(
        "--detect-anchor",
        choices=("center", "bottom-center"),
        default="bottom-center",
        help="How to place detected sprites on the output canvas. Default: bottom-center",
    )

    args = parser.parse_args()

    split_shimeji_sheet(
        character_name=args.character_name,
        cols=args.cols,
        rows=args.rows,
        total_frames=args.frames,
        clear_old=not args.keep_old,
        crop_offsets=tuple(args.offset),
        crop_to=tuple(args.crop_to) if args.crop_to is not None else None,
        transparent_bg=not args.no_transparent_bg,
        white_threshold=args.white_threshold,
        auto_detect=args.auto_detect,
        canvas_size=tuple(args.canvas_size),
        detect_alpha_threshold=args.detect_alpha_threshold,
        min_component_area=args.min_component_area,
        merge_radius=args.merge_radius,
        detect_padding=args.detect_padding,
        detect_anchor=args.detect_anchor,
    )


if __name__ == "__main__":
    main()