"""信号与信息处理工具（DSP）—— 基于 numpy/scipy，让 Agent 能做底层信号处理。

能力：FFT 频谱、数字滤波（低通/高通/带通）、采样率转换、平滑、卷积、统计特征。
数据由模型以数字数组传入（来自传感器/串口/仿真/文件），工具只做真实计算，
返回可核验的数值结果，不编造数据。

这是"信息处理层面"的合法底层能力；不涉及驱动/内核修改、不监听硬件信号。
"""
from __future__ import annotations

import json
from pathlib import Path

from .tools import ToolResult


def _parse_signal(args):
    raw = args.get("signal", [])
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            raw = json.loads(s)
        else:
            raw = [float(x) for x in s.replace(",", " ").split()]
    import numpy as np
    signal = np.asarray(raw, dtype=float).ravel()
    if signal.size == 0:
        raise ValueError("信号数据为空。")
    if signal.size > 200000:
        raise ValueError("信号点数超过 200000，请采样后再传入。")
    return signal


def _top_peaks(freqs, magnitude, top):
    import numpy as np
    idx = np.argsort(magnitude)[-top:][::-1]
    return [{"freq_hz": round(float(freqs[i]), 3), "magnitude": round(float(magnitude[i]), 5)}
            for i in idx]


def register_dsp_tools(registry, confirm=None):
    BASE = Path(__file__).resolve().parent.parent

    def spectrum(args):
        import numpy as np
        from scipy import signal as sig
        x = _parse_signal(args)
        fs = float(args.get("fs", 1000) or 1000)
        top = max(1, min(int(args.get("top", 5) or 5), 20))
        nperseg = min(int(args.get("nperseg", 0) or x.size), x.size) or x.size
        freqs, psd = sig.welch(x, fs=fs, nperseg=nperseg)
        peaks = _top_peaks(freqs, psd, top)
        return ToolResult(True,
            f"信号长度 {x.size}，采样率 {fs}Hz。\n功率谱主峰（Welch 法）：\n" +
            "\n".join(f"  {i+1}. {p['freq_hz']} Hz，功率 {p['magnitude']}" for i, p in enumerate(peaks)) +
            "\n说明：基于真实输入数组计算；频率分辨率约 " + f"{fs/x.size:.3f} Hz。")

    def filt(args):
        import numpy as np
        from scipy import signal as sig
        x = _parse_signal(args)
        fs = float(args.get("fs", 1000) or 1000)
        kind = str(args.get("kind", "lowpass")).casefold()
        cutoff = args.get("cutoff", 50)
        nyq = fs / 2
        if kind in {"lowpass", "highpass"}:
            cutoff_hz = float(cutoff)
            if not 0 < cutoff_hz < nyq:
                return ToolResult(False, f"截止频率需在 0 与奈奎斯特频率 {nyq}Hz 之间。")
            b, a = sig.butter(4, cutoff_hz / nyq, btype=kind)
        elif kind in {"bandpass", "bandstop"}:
            low, high = (float(v) for v in cutoff) if isinstance(cutoff, list) and len(cutoff) == 2 else (float(cutoff), float(cutoff))
            if not 0 < low < high < nyq:
                return ToolResult(False, f"带通边界需满足 0 < low < high < {nyq}Hz。")
            b, a = sig.butter(4, [low / nyq, high / nyq], btype=kind)
        else:
            return ToolResult(False, "kind 必须是 lowpass/highpass/bandpass/bandstop。")
        y = sig.filtfilt(b, a, x)
        preview = [round(float(v), 4) for v in y[:12]]
        save = bool(args.get("save", False))
        info = ""
        if save:
            out = BASE / "runtime" / ("filtered_" + str(abs(hash((kind, str(cutoff), x.size))) % 100000) + ".json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps([float(v) for v in y]), encoding="utf-8")
            info = f"\n已保存完整结果：{out}"
        return ToolResult(True, f"滤波完成：{kind}，截止 {cutoff}，采样率 {fs}Hz。\n"
                                f"输入输出长度 {x.size}。前 12 个输出值：{preview}{info}\n"
                                "说明：零相位 filtfilt，4 阶 Butterworth；需要完整数组可 save=true。")

    def resample(args):
        import numpy as np
        from scipy import signal as sig
        x = _parse_signal(args)
        old_fs = float(args.get("fs", 1000) or 1000)
        new_fs = float(args.get("new_fs", 0) or 0)
        if new_fs <= 0:
            return ToolResult(False, "请提供目标采样率 new_fs。")
        num = max(1, int(round(x.size * new_fs / old_fs)))
        y = sig.resample(x, num)
        return ToolResult(True, f"重采样：{old_fs}Hz → {new_fs}Hz，点数 {x.size} → {y.size}。\n"
                                f"前 12 个值：{[round(float(v),4) for v in y[:12]]}")

    def stats(args):
        import numpy as np
        from scipy import signal as sig
        x = _parse_signal(args)
        result = {
            "count": int(x.size), "mean": round(float(np.mean(x)), 5),
            "std": round(float(np.std(x)), 5), "min": round(float(np.min(x)), 5),
            "max": round(float(np.max(x)), 5),
            "rms": round(float(np.sqrt(np.mean(x ** 2))), 5),
            "peak_to_peak": round(float(np.ptp(x)), 5),
        }
        fs = float(args.get("fs", 0) or 0)
        if fs > 0 and x.size >= 8:
            freqs, psd = sig.welch(x, fs=fs)
            result["dominant_freq_hz"] = round(float(freqs[int(np.argmax(psd))]), 3)
        return ToolResult(True, "信号统计特征：\n" + json.dumps(result, ensure_ascii=False, indent=2))

    def smooth(args):
        import numpy as np
        from scipy import signal as sig
        x = _parse_signal(args)
        window = max(2, min(int(args.get("window", 5) or 5), min(201, x.size)))
        y = sig.savgol_filter(x, window if window % 2 == 1 else window + 1, 2)
        return ToolResult(True, f"Savitzky-Golay 平滑（窗口 {window}）完成。\n"
                                f"前 12 个输出值：{[round(float(v),4) for v in y[:12]]}")

    for name, desc, params, fn in [
        ("dsp_spectrum", "对数字信号做 FFT/Welch 频谱分析，找主频和功率谱峰值。需传信号数组和采样率 fs",
         {"type": "object", "properties": {
             "signal": {"type": "array", "description": "信号数值数组，或逗号分隔字符串"},
             "fs": {"type": "number", "description": "采样率 Hz，默认 1000"},
             "top": {"type": "integer", "description": "返回前几个主峰，默认 5"},
         }, "required": ["signal"]}, spectrum),
        ("dsp_filter", "数字滤波：Butterworth 低通/高通/带通/带阻，零相位 filtfilt。信号数组 + 截止频率",
         {"type": "object", "properties": {
             "signal": {"type": "array", "description": "信号数值数组"},
             "fs": {"type": "number", "description": "采样率 Hz"},
             "kind": {"type": "string", "description": "lowpass/highpass/bandpass/bandstop"},
             "cutoff": {"description": "截止频率 Hz；带通用 [low, high]"},
             "save": {"type": "boolean", "description": "是否保存完整结果到文件"},
         }, "required": ["signal", "kind", "cutoff"]}, filt),
        ("dsp_resample", "改变信号采样率（重采样），如 1000Hz→100Hz",
         {"type": "object", "properties": {
             "signal": {"type": "array", "description": "信号数值数组"},
             "fs": {"type": "number", "description": "原采样率 Hz"},
             "new_fs": {"type": "number", "description": "目标采样率 Hz"},
         }, "required": ["signal", "new_fs"]}, resample),
        ("dsp_stats", "计算信号统计特征：均值、标准差、RMS、峰峰值、主频等",
         {"type": "object", "properties": {
             "signal": {"type": "array", "description": "信号数值数组"},
             "fs": {"type": "number", "description": "可选采样率，用于主频分析"},
         }, "required": ["signal"]}, stats),
        ("dsp_smooth", "Savitzky-Golay 平滑去噪，保留信号形状",
         {"type": "object", "properties": {
             "signal": {"type": "array", "description": "信号数值数组"},
             "window": {"type": "integer", "description": "窗口长度，默认 5"},
         }, "required": ["signal"]}, smooth),
    ]:
        registry.register(name, desc, params, fn, source="dsp")
