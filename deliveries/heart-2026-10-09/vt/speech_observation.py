"""轻量的音频观察：只读波形，不调用模型、不改声音，不把指令当测量值。"""
from typing import Any, Dict
import math
import time

import numpy as np


def text_characters(text: str) -> int:
    """排除标点和空白；英文字母、数字仍按字符计，不冒充音节数。"""
    return sum(char.isalnum() for char in text)


def _pitch(frame: np.ndarray, sample_rate: int) -> float | None:
    """FFT 加速的差分/累计归一化估计；无可靠周期时返回 None。"""
    values = frame - frame.mean()
    size = len(values)
    first, last = max(2, int(sample_rate / 800)), min(size // 2, int(sample_rate / 70))
    if first >= last:
        return None
    fft_size = 1 << (2 * size - 1).bit_length()
    spectrum = np.fft.rfft(values, n=fft_size)
    corr = np.fft.irfft(spectrum * spectrum.conj(), n=fft_size)[:last + 1]
    square = np.concatenate(([0.0], np.cumsum(values * values)))
    lags = np.arange(last + 1)
    difference = np.maximum(0, square[size - lags] + square[size] - square[lags] - 2 * corr)
    cumulative = np.cumsum(difference[1:])
    normalized = np.ones(last + 1)
    normalized[1:] = difference[1:] * lags[1:] / np.maximum(cumulative, 1e-15)
    hits = np.flatnonzero(normalized[first:last] < .18)
    if not len(hits):
        return None  # 噪声和清辅音不硬凑“音高”。
    lag = int(hits[0] + first)
    while lag < last - 1 and normalized[lag + 1] < normalized[lag]:
        lag += 1
    denominator = normalized[lag - 1] - 2 * normalized[lag] + normalized[lag + 1]
    offset = .5 * (normalized[lag - 1] - normalized[lag + 1]) / denominator if abs(denominator) > 1e-12 else 0.0
    frequency = sample_rate / (lag + float(np.clip(offset, -.5, .5)))
    return frequency if 70 <= frequency <= 800 else None


def observe_audio(waveform: np.ndarray, sample_rate: int, text: str,
                  settings: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """针对最终拼接的音频计算一次，长音频的音高帧有采样上限。"""
    started = time.perf_counter()
    settings = settings or {}
    slow = float(settings.get("slow_chars_per_second", 3.0))
    fast = float(settings.get("fast_chars_per_second", 5.0))
    if not 0 < slow < fast <= 20:
        raise ValueError("观察语速阈值需要满足 0 < 慢 < 快 <= 20")
    values = np.asarray(waveform, dtype=np.float64)
    if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
        raise ValueError("观察音频的采样率不支持")
    if values.ndim not in {1, 2} or not len(values) or not np.isfinite(values).all():
        raise ValueError("观察音频为空或含无效数值")
    channels = 1 if values.ndim == 1 else values.shape[1]
    if channels not in {1, 2}:
        raise ValueError("观察只支持单声道或双声道")
    # 双声道用能量较高的一侧估计音高，避免反相抵消；总幅度仍统计全部声道。
    signal = values if values.ndim == 1 else values[:, np.argmax(np.mean(values * values, axis=0))]
    duration = len(values) / sample_rate
    rms = float(np.sqrt(np.mean(values * values)))
    peak = float(np.max(np.abs(values)))
    count = text_characters(text)
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in text)
    result = dict(version=1, status="已测量", audio_seconds=round(duration, 3),
                  sample_rate_hz=sample_rate, channels=channels, text_characters=count,
                  rms_dbfs=round(20 * math.log10(rms), 1) if rms > 1e-9 else None,
                  peak_dbfs=round(20 * math.log10(peak), 1) if peak > 1e-9 else None,
                  clipping_percent=round(float(np.mean(np.abs(values) >= .999)) * 100, 3),
                  speed_thresholds={"slow": slow, "fast": fast})
    hop = max(1, round(sample_rate * .02))
    bins = np.array([np.sqrt(np.mean(signal[index:index + hop] ** 2))
                     for index in range(0, len(signal), hop)])
    # 低于 -50dBFS 或本段峰值帧低35dB的较大者，视为低能量；不是语义停顿识别。
    threshold = max(10 ** (-50 / 20), float(np.max(bins)) * 10 ** (-35 / 20))
    quiet = bins < threshold
    edges = np.diff(np.concatenate(([False], quiet, [False])).astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    gaps = [max(0, min(duration, end * hop / sample_rate) - start * hop / sample_rate)
            for start, end in zip(starts, ends) if (end - start) * hop / sample_rate >= .15]
    result.update(low_energy_percent=round(float(np.mean(quiet)) * 100, 1),
                  low_energy_gap_count=len(gaps), longest_low_energy_gap_seconds=round(max(gaps, default=0), 3))
    if rms < 10 ** (-55 / 20) or float(np.mean(quiet)) >= .95:
        result.update(chars_per_second=None, speed_description="音频近乎静音，不估计语速",
                      pitch_median_hz=None, pitch_range_hz=None, pitch_span_semitones=None)
    else:
        density = count / duration if count else None
        # 短句/混合语言容易误判，显示数字但不贴“偏慢/偏快”标签。
        label = "短句或混合语言，不分类"
        if count >= 8 and cjk / count >= .8 and duration >= 1.5 and density is not None:
            label = "偏慢" if density < slow else "偏快" if density > fast else "中等"
        result.update(chars_per_second=round(density, 2) if density is not None else None,
                      speed_description=label)
        frame_size = round(sample_rate * .06)
        frame_starts = np.arange(0, max(0, len(signal) - frame_size + 1), hop)
        if len(frame_starts) > 800:
            frame_starts = frame_starts[np.linspace(0, len(frame_starts) - 1, 800).astype(int)]
        pitches = []
        for index in frame_starts:
            frame = signal[index:index + frame_size]
            if np.sqrt(np.mean(frame * frame)) >= threshold:
                frequency = _pitch(frame, sample_rate)
                if frequency is not None:
                    pitches.append(frequency)
        result["pitch_frames"] = len(pitches)
        if len(pitches) >= 3:
            low, high = np.percentile(pitches, [5, 95])
            result.update(pitch_median_hz=round(float(np.median(pitches)), 1),
                          pitch_range_hz=[round(float(low), 1), round(float(high), 1)],
                          pitch_span_semitones=round(12 * math.log2(high / low), 1))
        else:
            result.update(pitch_median_hz=None, pitch_range_hz=None, pitch_span_semitones=None)
    result["analysis_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result
