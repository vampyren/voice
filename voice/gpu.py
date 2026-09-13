"""Can this install actually drive an NVIDIA card?

A card being present says nothing. The CPU-only package leaves the CUDA
libraries out on purpose, so `ctranslate2.get_cuda_device_count()` counts the
card and CTranslate2 then cannot load it - and, worse, a model *constructs* on
`cuda` perfectly well, because cuBLAS is only needed when it first computes.
That is how a machine with a 4090 got "Library libcublas.so.12 is not found or
cannot be loaded" at the end of its first dictation, past every fallback.

So the question is answered by looking for the library, before anything is
built. Both `voice doctor` and the transcriber ask here, so they cannot
disagree about whether the card is usable.
"""
from __future__ import annotations

import sys
from pathlib import Path

#: The one library whose absence defines the CPU-only build.
CUDA_LIBRARY = "libcublas.so.12"

#: Where the Arch package puts the runtime when it is built with VOICE_GPU=1.
PACKAGED_CUDA = Path("/usr/lib/voice/cuda")


def bundled_cuda_runtime() -> bool:
    """Is the CUDA runtime installed where this process would find it?

    Looked for on this process's own paths rather than at a hardcoded
    location: hardcoding the package path told every source install, with a
    working GPU in use, that it was the CPU-only build.
    """
    import site

    roots = [PACKAGED_CUDA]
    try:
        roots += [Path(p) for p in site.getsitepackages()]
    except Exception:
        pass
    roots += [Path(p) for p in sys.path if p]
    for root in roots:
        try:
            if any(root.glob(f"nvidia/*/lib/{CUDA_LIBRARY}")):
                return True
        except OSError:
            continue
    return False
