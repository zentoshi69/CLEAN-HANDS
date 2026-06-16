"""Generates a clean PLACEHOLDER backdrop (assets/scene-dubai.jpg). Replace it
with the real photoreal art (gloved hands + city, no UI) for the final look."""
from PIL import Image, ImageDraw, ImageFilter
W, H = 900, 1600
img = Image.new("RGB", (W, H), (200, 228, 250))
px = img.load()

def lerp(a, b, t): return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
# vertical multi-stop gradient: sky -> haze -> water -> marble
stops = [(0.0,(146,199,244)),(0.40,(199,227,250)),(0.60,(232,244,255)),
         (0.66,(150,210,224)),(0.72,(120,198,216)),(0.74,(244,248,252)),(1.0,(214,224,236))]
for y in range(H):
    t = y / H
    for i in range(len(stops)-1):
        if stops[i][0] <= t <= stops[i+1][0]:
            seg = (t-stops[i][0])/max(1e-6,(stops[i+1][0]-stops[i][0]))
            c = lerp(stops[i][1], stops[i+1][1], seg); break
    else:
        c = stops[-1][1]
    for x in range(W): px[x, y] = c

# soft sun glow upper-centre
glow = Image.new("L", (W, H), 0)
gd = ImageDraw.Draw(glow)
gd.ellipse([W//2-280, int(H*0.02), W//2+280, int(H*0.34)], fill=120)
glow = glow.filter(ImageFilter.GaussianBlur(80))
img.paste(Image.new("RGB", (W, H), (255, 255, 255)), (0, 0), glow)

# skyline silhouette (translucent), sitting on the waterline ~0.66H
sky = Image.new("RGBA", (W, H), (0, 0, 0, 0))
sd = ImageDraw.Draw(sky)
base = int(H*0.66)
towers = [(40,300),(120,360),(190,250),(250,430),(330,300),  # x,height
          (560,320),(630,250),(690,420),(770,300),(840,360)]
for x, h in towers:
    sd.rounded_rectangle([x, base-h, x+58, base], radius=8, fill=(150,184,228,150))
# central spire (Burj-like)
sd.polygon([(W//2-12, base), (W//2, base-560), (W//2+12, base)], fill=(176,204,236,170))
sd.rectangle([W//2-4, base-610, W//2+4, base-560], fill=(176,204,236,170))
sky = sky.filter(ImageFilter.GaussianBlur(1.5))
img.paste(sky, (0, 0), sky)

# marble sheen lines on the floor
fd = ImageDraw.Draw(img, "RGBA")
for i in range(-2, 14):
    fd.line([(i*90, int(H*0.78)), (i*90+260, H)], fill=(190,205,222,70), width=2)

img.filter(ImageFilter.GaussianBlur(0.4)).save("assets/scene-dubai.jpg", quality=86)
print("wrote assets/scene-dubai.jpg")
