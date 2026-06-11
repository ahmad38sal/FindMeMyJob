# Icons

The manifest references `icon-16.png`, `icon-48.png`, `icon-128.png` here.
They aren't shipped — drop in your own, or generate solid-color placeholders:

```bash
for s in 16 48 128; do
  magick -size ${s}x${s} xc:'#0a84ff' "icon-${s}.png"
done
```

(`magick` from ImageMagick. On macOS: `brew install imagemagick`.)
