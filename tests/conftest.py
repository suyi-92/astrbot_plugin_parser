import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("parser_under_test")
package.__path__ = [str(ROOT)]
sys.modules["parser_under_test"] = package

from parser_under_test.core.config import ParserConfig


@pytest.fixture
def cfg(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    return SimpleNamespace(
        cache_dir=cache,
        data_dir=tmp_path,
        cookie_dir=cookies,
        source_max_size=90,
        max_size=90 * 1024**2,
        max_duration=900,
        download_retry_times=2,
        download_timeout=10,
        common_timeout=5,
        proxy=None,
        debug_media_info=False,
        save_config=Mock(),
        parser=ParserConfig(
            [
                {
                    "__template_key": "bilibili",
                    "enable": True,
                    "use_proxy": False,
                    "cookies": "",
                    "block_pcdn": False,
                    "video_quality": "_1080P_60",
                    "video_codec_list": ["AVC"],
                }
            ]
        ),
    )
