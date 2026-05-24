"""Match user HEIC photos to enriched rows via perceptual hashing.

Registers pillow-heif at startup so PIL.Image.open handles HEIC files
transparently. Computes imagehash.phash for each user photo and each row's
sbwsz reference image, then assigns each photo to the row with smallest
Hamming distance. Distances in the uncertain band fall back to an Anthropic
vision-model call (HEIC re-encoded as JPEG in memory before the API call).
Writes matched.json (photo_path added per row) and unmatched.json for photos
that could not be confidently assigned.
"""


def main() -> None:
    raise NotImplementedError()
