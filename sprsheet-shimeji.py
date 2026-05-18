#!/usr/bin/env python3

from pathlib import Path
from PIL import Image
import argparse


def make_sprite_sheet(
    input_dir: str,
    output_path: str,
    cols: int = 6,
    rows: int = 8,
    frame_width: int = 128,
    frame_height: int = 128,
    total_frames: int = 46,
    resize: bool = False,
):
    """
    Combines shime1.png through shime46.png into a transparent sprite sheet.

    Default output:
        6 columns x 8 rows

    For 128x128 frames, output size will be:
        768x1024
    """

    input_dir = Path(input_dir)
    output_path = Path(output_path)

    if not input_dir.is_dir():
        raise FileNotFoundError(f"Could not find input directory: {input_dir}")

    sheet_width = cols * frame_width
    sheet_height = rows * frame_height

    sprite_sheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 0))

    max_slots = cols * rows

    if total_frames > max_slots:
        raise ValueError(
            f"total_frames={total_frames} will not fit in a "
            f"{cols}x{rows} sprite sheet with only {max_slots} slots."
        )

    for frame_number in range(1, total_frames + 1):
        frame_path = input_dir / f"shime{frame_number}.png"

        if not frame_path.exists():
            print(f"Warning: missing {frame_path}, leaving that slot transparent.")
            continue

        frame = Image.open(frame_path).convert("RGBA")

        if frame.size != (frame_width, frame_height):
            if resize:
                frame = frame.resize((frame_width, frame_height), Image.Resampling.LANCZOS)
            else:
                raise ValueError(
                    f"{frame_path} is {frame.size[0]}x{frame.size[1]}, expected "
                    f"{frame_width}x{frame_height}. Use --resize to force resize."
                )

        index = frame_number - 1
        col = index % cols
        row = index // cols

        x = col * frame_width
        y = row * frame_height

        sprite_sheet.paste(frame, (x, y), frame)

        print(f"Placed {frame_path} at row {row + 1}, column {col + 1}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sprite_sheet.save(output_path)

    print(f"Done! Saved sprite sheet to: {output_path}")
    print(f"Sprite sheet size: {sheet_width}x{sheet_height}")


def main():
    parser = argparse.ArgumentParser(
        description="Combine Shimeji shime1.png through shime46.png into a sprite sheet."
    )

    parser.add_argument(
        "character_name",
        help='Character name, e.g. "johnny" for johnny-shimeji/img/Shimeji',
    )

    parser.add_argument(
        "--input-dir",
        default=None,
        help='Input directory. Default: "{character_name}-shimeji/img/Shimeji"',
    )

    parser.add_argument(
        "--output",
        default=None,
        help='Output sprite sheet path. Default: "{character_name}-spritesheet.png"',
    )

    parser.add_argument(
        "--cols",
        type=int,
        default=6,
        help="Number of columns in the sprite sheet. Default: 6",
    )

    parser.add_argument(
        "--rows",
        type=int,
        default=8,
        help="Number of rows in the sprite sheet. Default: 8",
    )

    parser.add_argument(
        "--frame-width",
        type=int,
        default=128,
        help="Frame width in pixels. Default: 128",
    )

    parser.add_argument(
        "--frame-height",
        type=int,
        default=128,
        help="Frame height in pixels. Default: 128",
    )

    parser.add_argument(
        "--frames",
        type=int,
        default=46,
        help="Total number of frames to import. Default: 46",
    )

    parser.add_argument(
        "--resize",
        action="store_true",
        help="Resize frames to the target frame size if they are not already correct.",
    )

    args = parser.parse_args()

    input_dir = args.input_dir
    if input_dir is None:
        input_dir = f"{args.character_name}-shimeji/img/Shimeji"

    output = args.output
    if output is None:
        output = f"{args.character_name}-spritesheet.png"

    make_sprite_sheet(
        input_dir=input_dir,
        output_path=output,
        cols=args.cols,
        rows=args.rows,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        total_frames=args.frames,
        resize=args.resize,
    )


if __name__ == "__main__":
    main()