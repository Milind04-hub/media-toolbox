"""Vocal / instrumental separation using Demucs, with ffmpeg-based audio I/O.

We avoid torchaudio/torchcodec (which need ffmpeg shared DLLs) by decoding the
input to raw float32 PCM with the bundled ffmpeg, running the Demucs model on the
tensor directly, then re-encoding each stem with ffmpeg.
"""
import subprocess
import threading

import numpy as np
import torch

SAMPLERATE = 44100
CHANNELS = 2

_model = None
_model_lock = threading.Lock()


def get_device():
    return "cuda" if torch.cuda.is_available() else "cpu"


def get_demucs_model():
    global _model
    with _model_lock:
        if _model is None:
            from demucs.pretrained import get_model
            _model = get_model("htdemucs")
            _model.eval()
            _model.to(get_device())
        return _model


def _decode_to_tensor(ffmpeg, in_path):
    """Decode any audio/video file to a (channels, samples) float32 torch tensor."""
    cmd = [ffmpeg, "-v", "error", "-i", in_path,
           "-ac", str(CHANNELS), "-ar", str(SAMPLERATE),
           "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg decode failed: " + proc.stderr.decode(errors="ignore")[-400:])
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("no audio stream found in the uploaded file")
    audio = audio.reshape(-1, CHANNELS).T.copy()   # (channels, samples)
    return torch.from_numpy(audio)


def _encode_from_tensor(ffmpeg, tensor, out_path, out_fmt):
    """Encode a (channels, samples) float32 tensor to mp3/wav via ffmpeg."""
    raw = tensor.t().contiguous().numpy().astype(np.float32).tobytes()
    cmd = [ffmpeg, "-v", "error", "-y",
           "-f", "f32le", "-ar", str(SAMPLERATE), "-ac", str(CHANNELS), "-i", "-"]
    if out_fmt == "mp3":
        cmd += ["-codec:a", "libmp3lame", "-b:a", "256k"]
    cmd.append(out_path)
    proc = subprocess.run(cmd, input=raw, capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg encode failed: " + proc.stderr.decode(errors="ignore")[-400:])


def separate(ffmpeg, in_path, vocals_out, instrumental_out, out_fmt="mp3", progress=None):
    """Split in_path into a vocals file and an instrumental file."""
    from demucs.apply import apply_model

    model = get_demucs_model()
    if progress:
        progress("decoding audio...")
    wav = _decode_to_tensor(ffmpeg, in_path)

    # Demucs expects the mix normalized; it handles that internally via apply_model.
    ref = wav.mean(0)
    wav_in = (wav - ref.mean()) / (ref.std() + 1e-8)

    device = get_device()
    if progress:
        progress(f"separating on {device.upper()}...")
    with torch.no_grad():
        out = apply_model(model, wav_in[None], split=True, overlap=0.25,
                          progress=False, device=device)[0]
    out = out.cpu() * ref.std() + ref.mean()   # undo normalization (back on CPU for encoding)

    sources = model.sources                # e.g. ['drums','bass','other','vocals']
    vi = sources.index("vocals")
    vocals = out[vi]
    instrumental = torch.stack([out[i] for i in range(len(sources)) if i != vi]).sum(0)

    if progress:
        progress("encoding stems...")
    _encode_from_tensor(ffmpeg, vocals, vocals_out, out_fmt)
    _encode_from_tensor(ffmpeg, instrumental, instrumental_out, out_fmt)
