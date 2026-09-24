"""Tiny Sixel encoder: draw a real image inside a terminal (Windows Terminal >= 1.22).

    from sixelimg import sixel, cell_px
    data = sixel('logo.png', height_px=96)          # a str to write to the terminal
    sys.stdout.write('\\x1b7\\x1b[%d;%dH' % (row, col) + data + '\\x1b8')

Transparent pixels are left unpainted (P2=1), so the logo sits on whatever the
terminal background is. Palette: up to 64 colours (median cut), run-length coded.
"""
from PIL import Image

ESC = '\x1b'


def sixel(path_or_img, height_px=96, colors=64, bg=None):
    im = path_or_img if isinstance(path_or_img, Image.Image) else Image.open(path_or_img)
    im = im.convert('RGBA')
    w = max(1, round(im.width * height_px / im.height))
    im = im.resize((w, height_px), Image.LANCZOS)
    alpha = im.getchannel('A')
    rgb = Image.new('RGB', im.size, bg or (0, 0, 0))
    rgb.paste(im, mask=alpha)
    q = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = q.getpalette()[:colors * 3]
    px = q.load()
    a = alpha.load()
    W, H = q.size
    used = sorted({px[x, y] for y in range(H) for x in range(W) if a[x, y] >= 96})
    out = [ESC + 'P0;1;0q', '"1;1;%d;%d' % (W, H)]
    for i in used:
        r, g, b = pal[i * 3:i * 3 + 3]
        out.append('#%d;2;%d;%d;%d' % (i, round(r * 100 / 255), round(g * 100 / 255), round(b * 100 / 255)))
    for band in range(0, H, 6):
        rows = range(band, min(band + 6, H))
        first = True
        for c in used:
            line = []
            any_on = False
            for x in range(W):
                bits = 0
                for k, y in enumerate(rows):
                    if px[x, y] == c and a[x, y] >= 96:
                        bits |= 1 << k
                line.append(chr(63 + bits))
                any_on = any_on or bits
            if not any_on:
                continue
            # run-length encode
            s, i = [], 0
            while i < len(line):
                j = i
                while j < len(line) and line[j] == line[i]:
                    j += 1
                n = j - i
                s.append(('!%d%s' % (n, line[i])) if n > 3 else line[i] * n)
                i = j
            out.append(('' if first else '$') + '#%d' % c + ''.join(s))
            first = False
        out.append('-')
    out.append(ESC + '\\')
    return ''.join(out)
