"""VoxCPM (OpenBMB, Apache-2.0) local TTS adapter — Desktop edition companion.

Uses the voxcpm Python library (pip install voxcpm) directly — same lazy-import
pattern as local_sd. Requires a GPU (8 GB VRAM) for reasonable speed; CPU works
but is slow. Supports zero-shot voice cloning from a short reference audio and
voice design from a text description (30+ languages, 48 kHz output).

Install:  pip install voxcpm   (downloads the model from HuggingFace on first use)
Clone:    record a reference in VoiceStudio / any recorder, pass it as ref_audio
"""

from pathlib import Path

from core.providers.router import Provider, ProviderError

_model_cache: dict = {}


class VoxCPMProvider(Provider):
    adapter = "voxcpm"

    def _model(self):
        import torch

        key = self.cfg.get("model", "openbmb/VoxCPM2")
        if key not in _model_cache:
            from voxcpm import VoxCPM

            device = "cuda" if torch.cuda.is_available() else "cpu"
            _model_cache[key] = VoxCPM.from_pretrained(key, load_denoiser=False, device=device)
        return _model_cache[key]

    def execute(self, payload: dict) -> dict:
        out = payload.get("out_path")
        if not out:
            raise ProviderError("voxcpm: payload must contain out_path")
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        text = payload.get("text", "")
        if not text:
            raise ProviderError("voxcpm: payload text is empty")

        try:
            import soundfile as sf

            model = self._model()
            ref_wav = payload.get("ref_audio") or self.cfg.get("ref_audio", "")
            prompt_text = payload.get("ref_text") or self.cfg.get("ref_text", "")
            cfg_value = float(self.cfg.get("cfg_value", 2.0))
            timesteps = int(self.cfg.get("inference_timesteps", 10))

            if ref_wav and Path(ref_wav).exists():
                # zero-shot cloning from a reference audio sample
                wav = model.generate(
                    text=text, prompt_wav_path=str(ref_wav),
                    prompt_text=prompt_text or None,
                    cfg_value=cfg_value, inference_timesteps=timesteps)
            else:
                # voice design / default voice
                wav = model.generate(text=text, cfg_value=cfg_value,
                                     inference_timesteps=timesteps)

            import numpy as np

            audio = wav if isinstance(wav, np.ndarray) else np.array(wav)
            sr = model.tts_model.sample_rate
            sf.write(str(out_path), audio, sr)
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001 — surface as provider error
            raise ProviderError(f"voxcpm: {e}") from e

        return {"audio_path": str(out_path), "format": "wav"}
