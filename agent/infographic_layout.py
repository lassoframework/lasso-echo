"""Placement geometry applied before pixel review, independent of art direction."""
import io
from PIL import Image, ImageStat


def story_frame(image_bytes, size=(1080, 1920)):
    """Inset an intact artwork in the Story safe region without cropping/stretching.

    Astra designs the content panel freely. The destination canvas reserves the
    interface areas mechanically; the actual composed pixels still face review.
    """
    with Image.open(io.BytesIO(image_bytes)) as source:
        artwork = source.convert("RGB")
    width, height = size
    left, top = round(width * .06), round(height * .17)
    right, bottom = int(width * .94), int(height * .80)
    artwork.thumbnail((right-left, bottom-top), Image.Resampling.LANCZOS)
    # Use the artwork's own edge color, keeping margins consistent with its
    # art direction without extending text or inventing decorative objects.
    edge = source.convert("RGB").crop((0, 0, source.width, max(1, source.height//100)))
    background = tuple(round(v) for v in ImageStat.Stat(edge).median)
    canvas = Image.new("RGB", size, background)
    canvas.paste(artwork, (left + (right-left-artwork.width)//2,
                           top + (bottom-top-artwork.height)//2))
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()
