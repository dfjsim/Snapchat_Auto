import base64
import os

import pandas as pd

from scripts import ParseSnapchat_iOS as parse_snapchat_ios


def test_merge_cache_handles_mixed_media_context_dtypes(tmp_path, monkeypatch):
    cache_df = pd.DataFrame(
        {
            "CACHE_KEY": ["abc", "def"],
            "EXTERNAL_KEY": ["ext1", "ext2"],
            "MEDIA_CONTEXT_TYPE": [2, 3],
        }
    )
    content_df = pd.DataFrame(
        {
            "CACHE_KEY": ["abc", "ghi"],
            "EXTERNAL_KEY": ["ext1", "ext3"],
            "MEDIA_CONTEXT_TYPE": ["", 19],
        }
    )

    sccontent_dir = tmp_path / "sccontent"
    output_dir = tmp_path / "output"
    os.makedirs(sccontent_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAACklEQVR4nGMAAA3AAwAAHw7mqgAAAABJRU5ErkJggg=="
    )
    for name in ("abc", "def", "ghi"):
        with open(sccontent_dir / name, "wb") as handle:
            handle.write(png_bytes)

    monkeypatch.setattr(parse_snapchat_ios, "SCContentFolder", str(sccontent_dir) + os.sep)
    monkeypatch.setattr(parse_snapchat_ios, "outputDir", str(output_dir))

    result = parse_snapchat_ios.mergeCache(cache_df, content_df)

    assert isinstance(result, pd.DataFrame)
    assert list(result.columns[:3]) == ["CACHE_KEY", "EXTERNAL_KEY", "MEDIA_CONTEXT_TYPE"]
    assert len(result) == 4
