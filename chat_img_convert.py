from PIL import Image
import sys
import os
import shutil

char_loc = sys.argv[1]
edit_loc = char_loc + "edits/"
old_dir = char_loc+"_old/"
if not os.path.isdir(old_dir):
    os.mkdir(old_dir)

print(f"[CHARACTER DIR]: {char_loc}")
print(f"[EDIT DIR]: {edit_loc}")
print(f"[OLD DIR]: {old_dir}")

def add_trans():
    '''Adds transparency to backgrounds on the edit files'''
    edit_files = [f for f in os.listdir(edit_loc) if 'edit' not in f]
    edit_num = len(edit_files)
    for FILENAME in edit_files:
        INPUT_FILE = edit_loc+FILENAME
        OUTPUT_FILE = edit_loc + os.path.basename(FILENAME).split('.')[0] + '_edit.png'

        print(f"[IN]: {INPUT_FILE} -- [OUT]: {OUTPUT_FILE}")

        # Higher = removes more off-white / light gray pixels
        WHITE_THRESHOLD = 235

        img = Image.open(INPUT_FILE).convert("RGBA")

        pixels = img.load()
        width, height = img.size

        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]

                # Turn white / near-white pixels transparent
                if r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
                    pixels[x, y] = (r, g, b, 0)

        # Downscale to 128x128
        img = img.resize((128, 128), Image.Resampling.LANCZOS)

        # Save as PNG to preserve transparency
        img.save(OUTPUT_FILE)

    print(f"Removed transparency on {edit_num} files in {edit_loc}")


def replace_files():
    '''Goes into the character images folder (above edits) and replaces the edits with the new version'''
    edit_files = [f for f in os.listdir(edit_loc) if '_edit' in f]

    for ef in edit_files:
        og_file = ef.replace('_edit','')
        if not os.path.isfile(char_loc+og_file):
            print(f"   >> {char_loc}/{og_file} not found")
            continue

        # make a copy in the previous directory
        shutil.copy(edit_loc+ef, char_loc)

        # send the old version to the old_dir
        if(os.path.isfile(old_dir+og_file)):
            os.remove(old_dir+og_file)
        shutil.move(char_loc+og_file, old_dir)

        # change the name of the edit version and overwrite
        os.rename(char_loc+ef, char_loc+og_file)

    print(f"Replaced all files! Moved {len(edit_files)} to {old_dir}")


if __name__ == "__main__":
    add_trans()
    replace_files()