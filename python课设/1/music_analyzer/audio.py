"""纯标准库 WAV 音频特征提取。

算法刻意不依赖 NumPy/librosa，方便课程设计在任意安装了 Python 3.10+
的电脑上直接运行。实现包括：波形解码、FFT、节奏估计、音高检测、
调性匹配、响度/频谱统计与规则分类。
"""

from __future__ import annotations

import cmath
import math
import statistics
import struct
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


ANALYSIS_SAMPLE_RATE = 11_025
MAX_ANALYSIS_SECONDS = 180
MAX_DECODE_BYTES = 24 * 1024 * 1024
FRAME_SIZE = 1_024
HOP_SIZE = 512
NOTE_NAMES = ("C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B")
NOTE_NAMES_ASCII = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Krumhansl-Schmuckler 大调/小调音级模板。
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)


class AudioAnalysisError(ValueError):
    """音频无法读取或分析时抛出的可展示异常。"""


@dataclass(slots=True)
class AudioBuffer:
    samples: list[float]
    sample_rate: int
    source_sample_rate: int
    channels: int
    sample_width: int
    total_frames: int
    duration: float
    analyzed_seconds: float
    truncated: bool


@dataclass(slots=True)
class AnalysisResult:
    duration: float
    sample_rate: int
    channels: int
    sample_width: int
    tempo: float
    tempo_confidence: float
    key_name: str
    musical_mode: str
    key_confidence: float
    rms_db: float
    peak_db: float
    dynamic_range_db: float
    avg_pitch_hz: float
    pitch_note: str
    spectral_centroid: float
    spectral_rolloff: float
    zero_crossing_rate: float
    classification: str
    classification_confidence: float
    analyzed_seconds: float
    truncated: bool
    feature_data: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _decode_resample_pcm(
    raw: bytes,
    sample_width: int,
    channels: int,
    source_rate: int,
    target_rate: int,
) -> tuple[list[float], int]:
    """直接从交错 PCM 生成降采样单声道，避免巨大的中间 float 列表。

    降采样使用箱式低通平均，减少高频折叠；若探测到反相声道平均后几乎
    抵消，则自动选取能量最高的声道。
    """
    if sample_width not in (1, 2, 3, 4):
        raise AudioAnalysisError(f"暂不支持 {sample_width * 8} 位 WAV，请转换为 8/16/24/32 位 PCM WAV。")
    block_align = sample_width * channels
    if len(raw) % block_align:
        raise AudioAnalysisError("WAV 数据块不完整，文件可能已经截断或损坏。")
    frame_count = len(raw) // block_align
    if frame_count <= 0:
        return [], 0

    byte_view = memoryview(raw)
    native_little = sys.byteorder == "little"
    int16_view = byte_view.cast("h") if sample_width == 2 and native_little else None
    int32_view = byte_view.cast("i") if sample_width == 4 and native_little else None

    def sample_value(sample_index: int) -> float:
        if sample_width == 1:
            return (byte_view[sample_index] - 128) / 128.0
        if sample_width == 2:
            value = int16_view[sample_index] if int16_view is not None else struct.unpack_from("<h", raw, sample_index * 2)[0]
            return value / 32768.0
        offset = sample_index * sample_width
        if sample_width == 3:
            value = byte_view[offset] | (byte_view[offset + 1] << 8) | (byte_view[offset + 2] << 16)
            if value & 0x800000:
                value -= 1 << 24
            return value / 8_388_608.0
        value = int32_view[sample_index] if int32_view is not None else struct.unpack_from("<i", raw, offset)[0]
        return value / 2_147_483_648.0

    selected_channel: int | None = None
    if channels > 1:
        channel_energy = [0.0] * channels
        mixed_energy = 0.0
        probe_step = max(1, frame_count // 4_096)
        probe_count = 0
        for frame_index in range(0, frame_count, probe_step):
            values = [sample_value(frame_index * channels + channel) for channel in range(channels)]
            mixed = sum(values) / channels
            mixed_energy += mixed * mixed
            for channel, value in enumerate(values):
                channel_energy[channel] += value * value
            probe_count += 1
            if probe_count >= 4_096:
                break
        average_channel_energy = sum(channel_energy) / channels
        if average_channel_energy > 1e-12 and mixed_energy < average_channel_energy * 0.04:
            selected_channel = max(range(channels), key=lambda channel: channel_energy[channel])

    def mixed_frame(frame_index: int) -> float:
        base = frame_index * channels
        if selected_channel is not None:
            return sample_value(base + selected_channel)
        return sum(sample_value(base + channel) for channel in range(channels)) / channels

    if source_rate == target_rate:
        return [mixed_frame(index) for index in range(frame_count)], frame_count

    ratio = source_rate / target_rate
    output_length = max(1, int(frame_count / ratio))
    output: list[float] = []
    for output_index in range(output_length):
        start = min(frame_count - 1, int(output_index * ratio))
        end = min(frame_count, max(start + 1, int((output_index + 1) * ratio)))
        output.append(sum(mixed_frame(index) for index in range(start, end)) / (end - start))
    return output, frame_count


def load_wav(path: str | Path) -> AudioBuffer:
    """读取 WAV，并把最多前三分钟降采样为单声道分析缓冲区。"""
    audio_path = Path(path)
    try:
        with wave.open(str(audio_path), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            source_rate = reader.getframerate()
            total_frames = reader.getnframes()
            compression = reader.getcomptype()
            if compression != "NONE":
                raise AudioAnalysisError("仅支持未压缩的 PCM WAV 音频。")
            if channels < 1 or channels > 8:
                raise AudioAnalysisError(f"不支持 {channels} 声道音频。")
            if source_rate < 4_000 or source_rate > 384_000:
                raise AudioAnalysisError(f"采样率 {source_rate} Hz 超出支持范围。")
            # 除时长外再限制解码字节数，避免极高采样率/多声道文件在纯
            # Python list 中膨胀并占用过多内存；文件总时长仍按头信息保存。
            byte_limited_frames = max(1, MAX_DECODE_BYTES // max(1, channels * sample_width))
            frames_to_read = min(total_frames, source_rate * MAX_ANALYSIS_SECONDS, byte_limited_frames)
            raw = reader.readframes(frames_to_read)
    except (wave.Error, EOFError) as exc:
        raise AudioAnalysisError("文件不是有效的 PCM WAV，或文件已经损坏。") from exc
    except OSError as exc:
        raise AudioAnalysisError(f"无法读取音频文件：{exc}") from exc

    if total_frames <= 0 or not raw:
        raise AudioAnalysisError("音频文件中没有可分析的采样。")

    analysis_rate = min(source_rate, ANALYSIS_SAMPLE_RATE)
    samples, decoded_frames = _decode_resample_pcm(raw, sample_width, channels, source_rate, analysis_rate)
    short_read = decoded_frames < frames_to_read
    duration = (decoded_frames if short_read else total_frames) / source_rate
    analyzed_seconds = decoded_frames / source_rate
    if len(samples) < 512:
        raise AudioAnalysisError("音频太短，请上传至少 0.1 秒的 WAV 文件。")

    return AudioBuffer(
        samples=samples,
        sample_rate=analysis_rate,
        source_sample_rate=source_rate,
        channels=channels,
        sample_width=sample_width,
        total_frames=total_frames,
        duration=duration,
        analyzed_seconds=analyzed_seconds,
        truncated=frames_to_read < total_frames or short_read,
    )


def _frames(samples: Sequence[float], frame_size: int = FRAME_SIZE, hop: int = HOP_SIZE) -> list[list[float]]:
    if len(samples) <= frame_size:
        padded = list(samples) + [0.0] * (frame_size - len(samples))
        return [padded]
    return [list(samples[start : start + frame_size]) for start in range(0, len(samples) - frame_size + 1, hop)]


def _rms(values: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def _db(amplitude: float) -> float:
    return 20.0 * math.log10(max(amplitude, 1e-10))


def _percentile(values: Sequence[float], proportion: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * max(0.0, min(1.0, proportion))
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _fft(values: Sequence[float]) -> list[complex]:
    """迭代 Cooley-Tukey FFT；输入长度必须是 2 的幂。"""
    size = len(values)
    if size == 0 or size & (size - 1):
        raise ValueError("FFT 输入长度必须是 2 的幂")
    output = [complex(value, 0.0) for value in values]
    target = 0
    for index in range(1, size):
        bit = size >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target ^= bit
        if index < target:
            output[index], output[target] = output[target], output[index]

    length = 2
    while length <= size:
        root = cmath.exp(-2j * math.pi / length)
        half = length // 2
        for start in range(0, size, length):
            weight = 1 + 0j
            for offset in range(half):
                even = output[start + offset]
                odd = output[start + offset + half] * weight
                output[start + offset] = even + odd
                output[start + offset + half] = even - odd
                weight *= root
        length <<= 1
    return output


def _selected_indices(count: int, maximum: int) -> list[int]:
    if count <= maximum:
        return list(range(count))
    return sorted({round(index * (count - 1) / (maximum - 1)) for index in range(maximum)})


def _estimate_tempo(frame_rms: Sequence[float], sample_rate: int) -> tuple[float, float, list[float]]:
    if len(frame_rms) < 8:
        return 0.0, 0.0, [0.0 for _ in frame_rms]

    log_energy = [math.log1p(value * 100.0) for value in frame_rms]
    onset = [0.0]
    for index in range(1, len(log_energy)):
        local_start = max(0, index - 8)
        local_mean = sum(log_energy[local_start:index]) / max(1, index - local_start)
        onset.append(max(0.0, log_energy[index] - max(log_energy[index - 1], local_mean)))

    maximum = max(onset, default=0.0)
    if maximum <= 1e-8:
        return 0.0, 0.0, [0.0 for _ in onset]
    normalized = [value / maximum for value in onset]
    frame_rate = sample_rate / HOP_SIZE
    minimum_lag = max(1, round(60.0 * frame_rate / 200.0))
    maximum_lag = min(len(normalized) - 2, round(60.0 * frame_rate / 55.0))
    if maximum_lag <= minimum_lag:
        return 0.0, 0.0, normalized

    candidates: list[tuple[float, int]] = []
    total_energy = sum(value * value for value in normalized) + 1e-9
    for lag in range(minimum_lag, maximum_lag + 1):
        numerator = sum(normalized[index] * normalized[index - lag] for index in range(lag, len(normalized)))
        overlap_scale = len(normalized) / max(1, len(normalized) - lag)
        correlation = (numerator / total_energy) * overlap_scale
        bpm = 60.0 * frame_rate / lag
        # 课程演示音乐通常位于 75–160 BPM，轻微先验可避免误选半拍/双拍。
        prior = 0.88 + 0.12 * math.exp(-((bpm - 115.0) / 55.0) ** 2)
        candidates.append((correlation * prior, lag))

    best_score, best_lag = max(candidates)
    refined_lag = float(best_lag)
    score_by_lag = {lag: score for score, lag in candidates}
    if best_lag - 1 in score_by_lag and best_lag + 1 in score_by_lag:
        left = score_by_lag[best_lag - 1]
        center = score_by_lag[best_lag]
        right = score_by_lag[best_lag + 1]
        denominator = left - 2 * center + right
        if abs(denominator) > 1e-12:
            offset = 0.5 * (left - right) / denominator
            refined_lag += max(-0.5, min(0.5, offset))
    tempo = 60.0 * frame_rate / refined_lag
    confidence = max(0.0, min(1.0, best_score * 2.2))
    if confidence < 0.08:
        return 0.0, confidence, normalized
    return round(tempo, 1), round(confidence, 3), normalized


def _estimate_pitch(frame: Sequence[float], sample_rate: int) -> tuple[float, float]:
    """用归一化自相关估计单帧基频及可信度。"""
    mean = sum(frame) / len(frame)
    centered = [value - mean for value in frame]
    energy = sum(value * value for value in centered)
    if energy / len(centered) < 1e-6:
        return 0.0, 0.0

    minimum_lag = max(2, int(sample_rate / 1_000.0))
    maximum_lag = min(len(centered) // 2, int(sample_rate / 55.0))
    scores: list[tuple[int, float]] = []
    for lag in range(minimum_lag, maximum_lag + 1):
        numerator = 0.0
        left_energy = 0.0
        right_energy = 0.0
        upper = len(centered) - lag
        for index in range(upper):
            left = centered[index]
            right = centered[index + lag]
            numerator += left * right
            left_energy += left * left
            right_energy += right * right
        denominator = math.sqrt(left_energy * right_energy) + 1e-12
        scores.append((lag, numerator / denominator))

    if not scores:
        return 0.0, 0.0
    best_lag, best_score = max(scores, key=lambda item: item[1])
    # 从接近最优的局部峰中优先选较小 lag，降低把基频误判成低八度的概率。
    threshold = max(0.55, best_score * 0.92)
    for index in range(1, len(scores) - 1):
        lag, score = scores[index]
        if score >= threshold and score >= scores[index - 1][1] and score >= scores[index + 1][1]:
            best_lag, best_score = lag, score
            break
    if best_score < 0.28:
        return 0.0, max(0.0, best_score)
    return sample_rate / best_lag, max(0.0, min(1.0, best_score))


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_norm = math.sqrt(sum((a - left_mean) ** 2 for a in left))
    right_norm = math.sqrt(sum((b - right_mean) ** 2 for b in right))
    return numerator / (left_norm * right_norm + 1e-12)


def _detect_key(chroma: Sequence[float]) -> tuple[str, str, float]:
    if not chroma or sum(chroma) <= 1e-10:
        return "未知", "未知", 0.0
    candidates: list[tuple[float, int, str]] = []
    for tonic in range(12):
        rotated_major = [MAJOR_PROFILE[(pitch_class - tonic) % 12] for pitch_class in range(12)]
        rotated_minor = [MINOR_PROFILE[(pitch_class - tonic) % 12] for pitch_class in range(12)]
        candidates.append((_pearson(chroma, rotated_major), tonic, "大调"))
        candidates.append((_pearson(chroma, rotated_minor), tonic, "小调"))
    candidates.sort(reverse=True)
    best, tonic, mode = candidates[0]
    second = candidates[1][0]
    confidence = max(0.0, min(1.0, 0.45 + (best - second) * 1.8 + max(0.0, best) * 0.25))
    return f"{NOTE_NAMES[tonic]} {mode}", mode, round(confidence, 3)


def _note_from_frequency(frequency: float) -> str:
    if frequency <= 0:
        return "未知"
    midi = round(69 + 12 * math.log2(frequency / 440.0))
    octave = midi // 12 - 1
    return f"{NOTE_NAMES[midi % 12]}{octave}"


def _waveform_points(samples: Sequence[float], bucket_count: int = 600) -> list[dict[str, float]]:
    if not samples:
        return []
    bucket_size = max(1, math.ceil(len(samples) / bucket_count))
    points: list[dict[str, float]] = []
    for start in range(0, len(samples), bucket_size):
        bucket = samples[start : start + bucket_size]
        points.append({"min": round(min(bucket), 4), "max": round(max(bucket), 4)})
    return points


def _spectrum_bands(spectrum: Sequence[float], sample_rate: int, band_count: int = 64) -> list[dict[str, float]]:
    if not spectrum:
        return []
    nyquist = sample_rate / 2
    minimum_frequency = 40.0
    maximum_frequency = min(5_000.0, nyquist)
    result: list[dict[str, float]] = []
    for band in range(band_count):
        lower = minimum_frequency * (maximum_frequency / minimum_frequency) ** (band / band_count)
        upper = minimum_frequency * (maximum_frequency / minimum_frequency) ** ((band + 1) / band_count)
        lower_bin = max(1, int(lower * FRAME_SIZE / sample_rate))
        upper_bin = min(len(spectrum), max(lower_bin + 1, int(upper * FRAME_SIZE / sample_rate)))
        magnitude = sum(spectrum[lower_bin:upper_bin]) / max(1, upper_bin - lower_bin)
        result.append({"frequency": round(math.sqrt(lower * upper), 1), "magnitude": magnitude})
    maximum = max((entry["magnitude"] for entry in result), default=1.0) or 1.0
    for entry in result:
        entry["magnitude"] = round(entry["magnitude"] / maximum, 4)
    return result


def _classify(tempo: float, rms_db: float, centroid: float, dynamic_range: float) -> tuple[str, float, list[str]]:
    reasons: list[str] = []
    if rms_db < -55:
        label = "静音或近静音"
        score = 0.95
        reasons.extend(["有效声音能量极低", "无法可靠判断节奏与音调"])
    elif tempo >= 125 and rms_db > -24:
        label = "激情动感"
        score = 0.78 + min(0.15, (tempo - 125) / 250)
        reasons.extend(["节奏速度较快", "整体能量较强"])
    elif tempo and tempo < 85 and rms_db < -22:
        label = "安静舒缓"
        score = 0.80
        reasons.extend(["节奏速度舒缓", "平均响度偏低"])
    elif centroid > 2_100 and (tempo == 0 or tempo >= 95):
        label = "明亮轻快"
        score = 0.76
        reasons.extend(["高频成分较丰富", "节奏较为轻快"])
    elif centroid < 1_250 and (tempo == 0 or tempo < 115):
        label = "温暖沉稳"
        score = 0.74
        reasons.extend(["频谱重心偏低", "节奏较为平稳"])
    else:
        label = "均衡流行"
        score = 0.70
        reasons.extend(["速度处于常见范围", "频谱与响度较均衡"])
    if dynamic_range > 16:
        reasons.append("动态变化明显")
        score += 0.03
    return label, round(min(0.95, score), 3), reasons


def analyze_wav(path: str | Path) -> AnalysisResult:
    """分析 PCM WAV 并返回结构化音乐特征。"""
    audio = load_wav(path)
    samples = audio.samples
    audio_frames = _frames(samples)
    frame_rms = [_rms(frame) for frame in audio_frames]
    # 一阶差分能量对鼓点/敲击等瞬态更敏感，比纯音量包络更适合估计 BPM。
    frame_transient_energy = [
        _rms([frame[index] - frame[index - 1] for index in range(1, len(frame))])
        for frame in audio_frames
    ]
    frame_db = [_db(value) for value in frame_rms if value > 1e-8]
    overall_rms = _rms(samples)
    peak = max(abs(value) for value in samples)
    rms_db = _db(overall_rms)
    peak_db = _db(peak)
    dynamic_range = (_percentile(frame_db, 0.90) - _percentile(frame_db, 0.10)) if frame_db else 0.0

    zero_crossings = 0
    for previous, current in zip(samples, samples[1:]):
        if (previous < 0 <= current) or (previous >= 0 > current):
            zero_crossings += 1
    zcr = zero_crossings / max(1, len(samples) - 1)

    tempo, tempo_confidence, onset_curve = _estimate_tempo(frame_transient_energy, audio.sample_rate)

    window = [0.5 - 0.5 * math.cos(2 * math.pi * index / (FRAME_SIZE - 1)) for index in range(FRAME_SIZE)]
    selected = _selected_indices(len(audio_frames), 140)
    average_spectrum = [0.0] * (FRAME_SIZE // 2 + 1)
    chroma = [0.0] * 12
    pitch_values: list[tuple[float, float]] = []
    analyzed_spectral_frames = 0

    energy_gate = max(1e-5, _percentile(frame_rms, 0.35) * 0.55)
    for frame_index in selected:
        frame = audio_frames[frame_index]
        if frame_rms[frame_index] < energy_gate:
            continue
        windowed = [value * window[index] for index, value in enumerate(frame)]
        fft_values = _fft(windowed)
        magnitudes = [abs(value) for value in fft_values[: FRAME_SIZE // 2 + 1]]
        for index, magnitude in enumerate(magnitudes):
            average_spectrum[index] += magnitude
            frequency = index * audio.sample_rate / FRAME_SIZE
            if 55.0 <= frequency <= min(5_000.0, audio.sample_rate / 2):
                midi = round(69 + 12 * math.log2(frequency / 440.0))
                chroma[midi % 12] += (magnitude * magnitude) / math.sqrt(max(frequency, 1.0))
        analyzed_spectral_frames += 1

    if analyzed_spectral_frames:
        average_spectrum = [value / analyzed_spectral_frames for value in average_spectrum]

    # 自相关比 FFT 更适合给出可讲解的基频；限制帧数保证纯 Python 速度。
    pitch_indices = _selected_indices(len(audio_frames), 56)
    for frame_index in pitch_indices:
        if frame_rms[frame_index] < energy_gate:
            continue
        frequency, confidence = _estimate_pitch(audio_frames[frame_index], audio.sample_rate)
        if 55.0 <= frequency <= 1_000.0 and confidence >= 0.30:
            pitch_values.append((frequency, confidence))

    if pitch_values:
        expanded = sorted(frequency for frequency, _ in pitch_values)
        average_pitch = statistics.median(expanded)
    else:
        average_pitch = 0.0

    frequencies = [index * audio.sample_rate / FRAME_SIZE for index in range(len(average_spectrum))]
    spectral_sum = sum(average_spectrum[1:]) + 1e-12
    centroid = sum(frequency * magnitude for frequency, magnitude in zip(frequencies[1:], average_spectrum[1:])) / spectral_sum
    cumulative = 0.0
    rolloff_target = spectral_sum * 0.85
    rolloff = 0.0
    for frequency, magnitude in zip(frequencies[1:], average_spectrum[1:]):
        cumulative += magnitude
        if cumulative >= rolloff_target:
            rolloff = frequency
            break

    chroma_sum = sum(chroma) or 1.0
    chroma_normalized = [value / chroma_sum for value in chroma]
    key_name, musical_mode, key_confidence = _detect_key(chroma_normalized)
    classification, classification_confidence, reasons = _classify(tempo, rms_db, centroid, dynamic_range)

    max_energy = max(frame_rms, default=1.0) or 1.0
    energy_curve = [round(value / max_energy, 4) for value in frame_rms]
    if len(energy_curve) > 420:
        indices = _selected_indices(len(energy_curve), 420)
        energy_curve = [energy_curve[index] for index in indices]
    if len(onset_curve) > 420:
        indices = _selected_indices(len(onset_curve), 420)
        onset_curve = [onset_curve[index] for index in indices]

    pitch_track = [
        {"frequency": round(frequency, 2), "confidence": round(confidence, 3)}
        for frequency, confidence in pitch_values
    ]
    feature_data = {
        "waveform": _waveform_points(samples),
        "energy": energy_curve,
        "onset": [round(value, 4) for value in onset_curve],
        "spectrum": _spectrum_bands(average_spectrum, audio.sample_rate),
        "chroma": [
            {"note": NOTE_NAMES_ASCII[index], "value": round(value, 5)}
            for index, value in enumerate(chroma_normalized)
        ],
        "pitch_track": pitch_track,
        "classification_reasons": reasons,
        "algorithm": {
            "tempo": "短时能量差分 + 自相关",
            "pitch": "归一化自相关基频检测",
            "key": "Chroma + Krumhansl-Schmuckler 模板匹配",
            "spectrum": "Hann 窗 + 1024 点 Cooley-Tukey FFT",
            "classification": "节奏、响度、频谱重心规则融合",
        },
    }

    return AnalysisResult(
        duration=round(audio.duration, 3),
        sample_rate=audio.source_sample_rate,
        channels=audio.channels,
        sample_width=audio.sample_width,
        tempo=tempo,
        tempo_confidence=tempo_confidence,
        key_name=key_name,
        musical_mode=musical_mode,
        key_confidence=key_confidence,
        rms_db=round(rms_db, 2),
        peak_db=round(peak_db, 2),
        dynamic_range_db=round(dynamic_range, 2),
        avg_pitch_hz=round(average_pitch, 2),
        pitch_note=_note_from_frequency(average_pitch),
        spectral_centroid=round(centroid, 2),
        spectral_rolloff=round(rolloff, 2),
        zero_crossing_rate=round(zcr, 5),
        classification=classification,
        classification_confidence=classification_confidence,
        analyzed_seconds=round(audio.analyzed_seconds, 3),
        truncated=audio.truncated,
        feature_data=feature_data,
    )


def generate_demo_wav(path: str | Path, seconds: float = 12.0, bpm: float = 120.0) -> Path:
    """生成带 C 大调和弦与节拍的演示音频，便于无素材时现场答辩。"""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 22_050
    frame_count = int(sample_rate * seconds)
    beat_interval = 60.0 / bpm
    melody = (261.63, 329.63, 392.00, 523.25, 392.00, 329.63)
    pcm = bytearray()
    for index in range(frame_count):
        time_value = index / sample_rate
        melody_index = int(time_value / beat_interval) % len(melody)
        frequency = melody[melody_index]
        chord = (
            0.34 * math.sin(2 * math.pi * frequency * time_value)
            + 0.18 * math.sin(2 * math.pi * 329.63 * time_value)
            + 0.15 * math.sin(2 * math.pi * 392.00 * time_value)
        )
        beat_phase = time_value % beat_interval
        click = 0.0
        if beat_phase < 0.055:
            click = 0.42 * math.exp(-beat_phase * 52) * math.sin(2 * math.pi * 1_100 * time_value)
        envelope = min(1.0, time_value * 4, max(0.0, (seconds - time_value) * 4))
        value = max(-0.98, min(0.98, (chord + click) * envelope))
        pcm.extend(struct.pack("<h", int(value * 32767)))
    with wave.open(str(output_path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(pcm))
    return output_path


def supported_wav_description() -> str:
    return "PCM WAV（8/16/24/32 位，1–8 声道，4 kHz–384 kHz）"
