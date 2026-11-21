"""Provider layer: base interface, registry, router (cache/budget/retries/fallback)."""

import hashlib
import json
import random
import time
from pathlib import Path

import yaml

from core.i18n import tr

PROVIDER_TYPES = ("llm", "tts", "stt", "image", "video", "music", "search", "publish")


class ProviderError(Exception):
    pass


class BudgetExceeded(ProviderError):
    pass


class Provider:
    """One configured service. `execute` must be deterministic-in-content for
    tests (mock) or a real remote call; router owns caching/retries."""

    adapter: str = "base"

    def __init__(self, name: str, ptype: str, cfg: dict, resolve_secret):
        self.name = name
        self.ptype = ptype
        self.cfg = cfg
        self._resolve_secret = resolve_secret
        self.model: str = cfg.get("model", "mock")

    def secret(self) -> str:
        ref = self.cfg.get("api_key_ref", "")
        return self._resolve_secret(ref) if ref else ""

    def execute(self, payload: dict) -> dict:
        raise NotImplementedError


def load_providers_config(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def build_registry(providers_cfg: dict, resolve_secret) -> dict[str, Provider]:
    """Map 'ptype/name' -> Provider from providers.yaml. Mock adapters come
    from core.providers.mock; anything else from core.providers.<adapter>."""
    from core.providers import chatterbox as chatterbox_mod
    from core.providers import f5_tts as f5_mod
    from core.providers import fish_audio as fish_mod
    from core.providers import local_sd as sd_mod
    from core.providers import mock as mock_mod
    from core.providers import library_music as libmus_mod
    from core.providers import ambient_synth as ambmus_mod
    from core.providers import openverse_music as opmus_mod
    from core.providers import freesound_music as frsmus_mod
    from core.providers import fal_video as falvid_mod
    from core.providers import openai_compatible as oai_mod
    from core.providers import jamendo_music as jamus_mod
    from core.providers import silero as silero_mod
    from core.providers import youtube_api as yt_mod
    from core.providers import xtts as xtts_mod
    from core.providers import vheer_browser as vheer_mod
    from core.providers import smallest_ai as smallest_mod
    from core.providers import voice_studio as vstudio_mod
    from core.providers import voxcpm as voxcpm_mod

    adapters = {
        "mock": mock_mod.MockProvider,
        "youtube_api": yt_mod.YoutubeApiProvider,
        "openai_compatible": oai_mod.OpenAICompatibleProvider,
        "chatterbox": chatterbox_mod.ChatterboxProvider,
        "f5_tts": f5_mod.F5TTSProvider,
        "fish_audio": fish_mod.FishAudioProvider,
        "library_music": libmus_mod.LibraryMusicProvider,
        "ambient_synth": ambmus_mod.AmbientSynthProvider,
        "openverse_music": opmus_mod.OpenverseMusicProvider,
        "freesound_music": frsmus_mod.FreesoundMusicProvider,
        "fal_video": falvid_mod.FalVideoProvider,
        "local_sd": sd_mod.LocalSDProvider,
        "jamendo_music": jamus_mod.JamendoMusicProvider,
        "silero": silero_mod.SileroProvider,
        "xtts": xtts_mod.XttsProvider,
        "vheer_browser": vheer_mod.VheerBrowserProvider,
        "smallest_ai": smallest_mod.SmallestAIProvider,
        "voice_studio": vstudio_mod.VoiceStudioProvider,
        "voxcpm": voxcpm_mod.VoxCPMProvider,
    }
    registry: dict[str, Provider] = {}
    for name, cfg in (providers_cfg.get("providers") or {}).items():
        adapter_name = cfg.get("adapter", "mock")
        if adapter_name not in adapters:
            raise ProviderError(f"unknown adapter '{adapter_name}' for provider '{name}'")
        p = adapters[adapter_name](name=name, ptype=cfg["type"], cfg=cfg, resolve_secret=resolve_secret)
        registry[f"{cfg['type']}/{name}"] = p
    return registry


class Router:
    """Single entry point for every external call: cache -> budget -> retries
    -> fallback chain -> usage accounting (architecture §3.2)."""

    def __init__(self, providers_cfg: dict, state, run_id: str, run_dir: Path, lang: str = "ru"):
        self.state = state
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.lang = lang
        self.registry = build_registry(providers_cfg, resolve_secret=_resolve_secret)
        self.defaults: dict[str, str] = providers_cfg.get("defaults") or {}
        self.budget_limit_usd: float = float(providers_cfg.get("budget_limit_usd", 0.0))
        self.fallbacks: dict[str, list[str]] = providers_cfg.get("fallbacks") or {}
        self.model_overrides: dict[str, str] = providers_cfg.get("_model_overrides") or {}
        self.pricing: dict = providers_cfg.get("_pricing") or {}
        if not self.pricing:
            try:
                import yaml as _yaml

                pp = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"
                if pp.exists():
                    self.pricing = _yaml.safe_load(pp.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 — pricing is optional
                pass
        self.on_event = lambda msg: None  # CLI may inject a printer

    # -- main entry ----------------------------------------------------------
    def call(self, ptype: str, payload: dict, provider_id: str | None = None,
             cost_usd: float = 0.0, tokens: int = 0) -> dict:
        chain = [provider_id or self._default(ptype)] + self.fallbacks.get(ptype, [])
        last_err: Exception | None = None
        for pid in chain:
            key = self._cache_key(ptype, pid, payload)
            cached = self.state.cache_get(key)
            if cached is not None:
                result = json.loads(cached)
                # file-producing providers: a cached path whose file was wiped
                # (runs/ cleanup) must be treated as a miss
                if all(Path(v).exists() for k, v in result.items() if k.endswith("_path")):
                    self.on_event(tr(self.lang, "cache_hit", ptype=ptype, provider=pid))
                    return result
            try:
                import copy as _copy

                provider = self._provider(ptype, pid)
                if pid == chain[0]:
                    mo = self.model_overrides.get(ptype)
                    if mo and provider.model != mo:
                        provider = _copy.copy(provider)  # per-run model switch, same key
                        provider.model = mo
                self._check_budget(provider, cost_usd)
                result = self._execute_with_retries(provider, payload)
            except ProviderError as e:
                last_err = e
                continue
            self.state.cache_put(key, ptype, provider.name, json.dumps(result, ensure_ascii=False))
            if isinstance(result, dict) and "usage" in result:
                tokens = int((result["usage"] or {}).get("total_tokens") or 0)
            if cost_usd <= 0:
                cost_usd = self._estimate_cost(ptype, result)
            self.state.usage_add(self.run_id, ptype, provider.name, cost_usd, tokens)
            self._usage_jsonl(ptype, provider.name, cost_usd, tokens)
            return result
        raise last_err or ProviderError(f"no provider for {ptype}")


    # -- internals -------------------------------------------------------------
    def _default(self, ptype: str) -> str:
        if ptype not in self.defaults:
            raise ProviderError(f"no default provider configured for type '{ptype}'")
        return self.defaults[ptype]

    def _provider(self, ptype: str, pid: str) -> Provider:
        key = f"{ptype}/{pid}"
        if key not in self.registry:
            raise ProviderError(f"provider '{key}' is not configured")
        return self.registry[key]

    def _cache_key(self, ptype: str, pid: str, payload: dict) -> str:
        provider = self.registry.get(f"{ptype}/{pid}")
        model = provider.model if provider else "?"
        basis = json.dumps(
            {"p": f"{ptype}/{pid}", "m": model, "payload": payload},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def _check_budget(self, provider: Provider, cost_usd: float):
        """Review fix: the budget used to be dead code (cost_usd was always 0).
        Now: paid calls need budget_limit_usd > 0 AND the cumulative recorded
        spend of this run (including pricing-based estimates) must fit."""
        spent = self.state.usage_spent(self.run_id)
        if cost_usd > 0:
            if self.budget_limit_usd <= 0:
                raise ProviderError(tr(self.lang, "paid_needs_flag", provider=provider.name))
            if spent + cost_usd > self.budget_limit_usd:
                raise BudgetExceeded(tr(
                    self.lang, "budget_stop",
                    spent=round(spent, 4), limit=self.budget_limit_usd,
                ))
        elif self.budget_limit_usd > 0 and spent >= self.budget_limit_usd:
            raise BudgetExceeded(tr(
                self.lang, "budget_stop",
                spent=round(spent, 4), limit=self.budget_limit_usd,
            ))

    def _estimate_cost(self, ptype: str, result: dict) -> float:
        """Pricing-based cost estimate (config/pricing.yaml) so the recorded
        spend and the budget are non-zero even for metered-by-usage APIs."""
        price = (self.pricing or {}).get(ptype) or {}
        if not price:
            return 0.0
        usage = (result or {}).get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)
        per_1m = price.get("usd_per_1m_tokens")
        if tokens and per_1m:
            return round(tokens / 1_000_000 * float(per_1m), 6)
        chars = price.get("usd_per_1k_chars")
        if chars and result.get("text"):
            return round(len(result["text"]) / 1000 * float(chars), 6)
        per_image = price.get("usd_per_image")
        if per_image and ptype == "image":
            return round(float(per_image), 6)
        return 0.0

    def _execute_with_retries(self, provider: Provider, payload: dict) -> dict:
        delay = 1.0
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                return provider.execute(payload)
            except ProviderError as e:
                last_err = e
                headers = getattr(e, "headers", None)
                retry_after = headers.get("retry-after") if headers else None
                time.sleep(float(retry_after) if retry_after else delay + random.uniform(0, 0.3))
                delay *= 2
        raise last_err  # type: ignore[misc]

    def _usage_jsonl(self, ptype: str, provider: str, cost_usd: float, tokens: int):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ptype": ptype, "provider": provider, "cost_usd": cost_usd, "tokens": tokens},
                          ensure_ascii=False)
        with open(self.run_dir / "usage.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _resolve_secret(ref: str) -> str:
    """Secrets come from the tenant's own .env (when a tenant context is
    active), else from environment/.env/keyring (keyring optional).

    Tenant isolation (review fix): inside a tenant context only tenant-scoped
    secrets are readable — host keys in os.environ are invisible. Outside a
    tenant, arbitrary refs are limited to the known provider-key allowlist so
    a crafted api_key_ref cannot read host environment variables."""
    import os
    from core.secrets import load_env

    from core import tenants as _tenants

    if _tenants.current_tenant():
        return _tenants.tenant_secret(Path(__file__).resolve().parents[2], ref)

    if ref.startswith("TENANT_") or ref not in ALLOWED_KEY_REFS:
        # not a known provider key: resolves to "" (review — a crafted
        # api_key_ref must not read arbitrary host environment variables)
        return ""

    if os.environ.get(ref):
        return os.environ[ref]

    # Search for .env in current directory or parent directory trees
    for env_cand in [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parents[3] / ".env",
    ]:
        if env_cand.exists():
            load_env(env_cand)
            if os.environ.get(ref):
                return os.environ[ref]

    try:
        import keyring

        val = keyring.get_password("pipeline", ref)
        if val:
            return val
    except Exception:
        pass
    return ""


# Known provider-key variable names (host/owner level). A provider entry that
# references anything outside this list resolves to "" unless a tenant-scoped
# key was stored for it — this blocks "point api_key_ref at any host variable"
# exfiltration (review).
ALLOWED_KEY_REFS = {
    "OPENROUTER_API_KEY", "B_AI_API_KEY", "SMALLEST_AI_API_KEY",
    "ELEVENLABS_API_KEY", "FISH_AUDIO_API_KEY", "CARTESIA_API_KEY",
    "JAMENDO_CLIENT_ID", "FREESOUND_TOKEN", "VOICESTUDIO_API_KEY",
    "FAL_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "PIXABAY_API_KEY", "TTS_API_KEY", "UI_TOKEN", "WORKER_TOKEN",
}
