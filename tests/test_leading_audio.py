"""Preserve the selected audio track's timing relative to the video's start."""
import array
import math
import shutil
import threading

import pytest
from conftest import TOOLS, needs_ffmpeg, decode_pcm
from mp4_audio_kit.converter import convert, ConvertError, Limits

@needs_ffmpeg
@pytest.mark.parametrize('name', ['leading.mp4', 'offset.mp4'])
def test_leading_audio_delay_is_silence_relative_to_file_start(media, tmp_path, name):
    src = tmp_path / 'input.mp4'; shutil.copyfile(media / name, src)
    output, seconds = convert(TOOLS, src, Limits(), threading.Event())
    samples = array.array('h', decode_pcm(output)); rate = 44100
    assert seconds == pytest.approx(8, abs=0.1)
    assert len(samples) / rate == pytest.approx(8, abs=0.1)
    silence = samples[int(.2*rate):int(2.7*rate)]
    assert math.sqrt(sum(x*x for x in silence) / len(silence)) < 5
    tone = samples[int(3.3*rate):int(7.7*rate)]
    assert math.sqrt(sum(x*x for x in tone) / len(tone)) > 500
    crossings = sum(a < 0 <= b for a,b in zip(tone, tone[1:]))
    assert crossings / (len(tone) / rate) == pytest.approx(440, abs=2)

@needs_ffmpeg
def test_leading_silence_counts_toward_the_duration_cap(media, tmp_path):
    src = tmp_path / 'input.mp4'; shutil.copyfile(media / 'leading.mp4', src)
    with pytest.raises(ConvertError) as err:
        convert(TOOLS, src, Limits(max_duration=6), threading.Event())
    assert err.value.code == 'too_long'
    assert not (tmp_path / 'output.mp3').exists()
    assert not (tmp_path / 'output.part.mp3').exists()
